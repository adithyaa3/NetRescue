"""Randomized, seed-reproducible property testing of the NetRescue backend.

Independent of the production validator: every invariant below is recomputed
from the raw API responses (topology, demands with tunnels, metrics, plan).

Reference optimum: an arc-based undirected multicommodity-flow LP solved
lexicographically per priority (P1..P5). It does not use candidate paths, so it
is an independent upper bound on any feasible allocation and, when every demand
has <= K simple paths, the exact optimum the Reactive LP must reach.

Usage:
  PYTHONPATH=backend .venv312/bin/python qa/property_fuzz.py [n_sequences] [ops_per_sequence] [first_seed]
Writes qa/backend-property-results.json. Exit code 1 if any invariant fails.
"""
from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import defaultdict, deque

import numpy as np
from fastapi.testclient import TestClient
from scipy.optimize import linprog

from app import main
from app.state import SimulationState

TOL = 1e-3
CATS = ["emergency", "essential", "academic", "general", "background"]
PRIO = {c: i + 1 for i, c in enumerate(CATS)}
K = 5


# ── Random scenario generation ───────────────────────────────────────

def random_scenario(rng: random.Random):
    n = rng.randint(3, 9 if rng.random() < 0.2 else 7)
    nodes = [f"N{i}" for i in range(n)]
    edges = set()
    connected = rng.random() < 0.85
    order = nodes[:]
    rng.shuffle(order)
    for i in range(1, n):
        if not connected and i == n - 1:
            break  # leave the last node isolated
        edges.add(tuple(sorted((order[i], order[rng.randrange(i)]))))
    for _ in range(rng.randint(0, n + 1)):
        a, b = rng.sample(nodes, 2)
        edges.add(tuple(sorted((a, b))))
    links = []
    for i, (a, b) in enumerate(sorted(edges)):
        links.append({"id": f"L{i + 1}", "source": a, "target": b,
                      "capacity": float(rng.choice([1, 5, 10, 20, 40, 50, 100])),
                      "latency": float(rng.randint(1, 9))})
    demands = []
    for i in range(rng.randint(1, 6)):
        a = rng.choice(nodes)
        b = rng.choice(nodes) if rng.random() < 0.1 else rng.choice([x for x in nodes if x != a])
        d = {"id": f"D{i + 1}", "source": a, "destination": b,
             "bandwidth": float(rng.choice([1, 3, 5, 10, 15, 25, 40, 60])),
             "category": rng.choice(CATS)}
        if rng.random() < 0.15:
            d["max_latency"] = float(rng.randint(3, 25))
        demands.append(d)
    return nodes, links, demands


# ── Independent graph helpers ─────────────────────────────────────────

def active_adj(links):
    adj = defaultdict(set)
    for l in links:
        if l["status"] == "active":
            adj[l["source"]].add(l["target"])
            adj[l["target"]].add(l["source"])
    return adj


def reachable(adj, s, t):
    if s == t:
        return True
    seen, q = {s}, deque([s])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v == t:
                return True
            if v not in seen:
                seen.add(v)
                q.append(v)
    return False


def simple_paths(links, s, t, limit=200):
    """Independent DFS enumeration of simple paths over active links (link-id lists)."""
    adj = defaultdict(list)
    for l in links:
        if l["status"] == "active":
            adj[l["source"]].append((l["target"], l["id"]))
            adj[l["target"]].append((l["source"], l["id"]))
    out = []

    def dfs(u, seen, path):
        if len(out) >= limit:
            return
        if u == t:
            out.append(list(path))
            return
        for v, lid in sorted(adj[u]):
            if v not in seen:
                seen.add(v)
                path.append(lid)
                dfs(v, seen, path)
                path.pop()
                seen.discard(v)
    dfs(s, {s}, [])
    return out


def path_latency(links, path_lids):
    lm = {l["id"]: l for l in links}
    return sum(lm[x]["latency"] for x in path_lids)


def path_lp_optimum(links, demands, paths, floors=None):
    """Lexicographic per-priority optimum over the given (latency-feasible) path sets,
    optionally subject to per-flow minimum rates (verified guarantees)."""
    act = {l["id"]: l for l in links if l["status"] == "active"}
    var = [(d["id"], i) for d in demands for i, _ in enumerate(paths.get(d["id"], []))]
    idx = {v: i for i, v in enumerate(var)}
    nv = len(var)
    result = {p: 0.0 for p in range(1, 6)}
    if nv == 0:
        return result
    A, b = [], []
    for d in demands:
        if paths.get(d["id"]):
            row = np.zeros(nv)
            for i, _ in enumerate(paths[d["id"]]):
                row[idx[(d["id"], i)]] = 1
            A.append(row); b.append(d["bandwidth"])
    for lid, l in act.items():
        row = np.zeros(nv)
        for d in demands:
            for i, pth in enumerate(paths.get(d["id"], [])):
                if lid in pth:
                    row[idx[(d["id"], i)]] = 1
        A.append(row); b.append(l["capacity"])
    for fid, g in (floors or {}).items():
        if g <= 1e-9 or not paths.get(fid):
            continue
        row = np.zeros(nv)
        for i, _ in enumerate(paths[fid]):
            row[idx[(fid, i)]] = -1
        A.append(row); b.append(-g + 1e-7)
    for p in range(1, 6):
        ids = [v for v in var if PRIO[next(d["category"] for d in demands if d["id"] == v[0])] == p]
        if not ids:
            continue
        c = np.zeros(nv)
        for v in ids:
            c[idx[v]] = -1
        res = None
        for slack in (0, 1e-6, 1e-5):
            if slack and A:
                b[-1] -= slack
            res = linprog(c, A_ub=np.array(A), b_ub=np.array(b), bounds=[(0, None)] * nv, method="highs")
            if res.success:
                break
        if not res.success:
            raise RuntimeError(f"path reference LP failed: {res.message}")
        result[p] = -res.fun
        row = np.zeros(nv)
        for v in ids:
            row[idx[v]] = -1
        A.append(row); b.append(-result[p] + 1e-6 * max(1.0, result[p]))
    return result


def arc_lp_optimum(nodes, links, demands):
    """Lexicographic per-priority maximum delivered throughput (independent reference)."""
    act = [l for l in links if l["status"] == "active"]
    flows = [d for d in demands if d["source"] != d["destination"]]
    if not flows or not act:
        return {p: 0.0 for p in range(1, 6)}
    arcs = []
    for l in act:
        arcs.append((l["source"], l["target"], l["id"]))
        arcs.append((l["target"], l["source"], l["id"]))
    na, nf = len(arcs), len(flows)
    nv = nf * na + nf  # arc flows + delivered rate r_k
    def fx(k, a):
        return k * na + a
    def rx(k):
        return nf * na + k
    A_eq, b_eq = [], []
    for k, d in enumerate(flows):
        for v in nodes:
            row = np.zeros(nv)
            for a, (u, w, _) in enumerate(arcs):
                if u == v:
                    row[fx(k, a)] += 1
                if w == v:
                    row[fx(k, a)] -= 1
            if v == d["source"]:
                row[rx(k)] = -1
            elif v == d["destination"]:
                row[rx(k)] = 1
            A_eq.append(row)
            b_eq.append(0.0)
    A_ub, b_ub = [], []
    for l in act:
        row = np.zeros(nv)
        for k in range(nf):
            for a, (_, _, lid) in enumerate(arcs):
                if lid == l["id"]:
                    row[fx(k, a)] = 1
        A_ub.append(row)
        b_ub.append(l["capacity"])
    bounds = [(0, None)] * (nf * na) + [(0, d["bandwidth"]) for d in flows]
    result = {}
    for p in range(1, 6):
        c = np.zeros(nv)
        idx = [k for k, d in enumerate(flows) if PRIO[d["category"]] == p]
        if not idx:
            result[p] = 0.0
            continue
        for k in idx:
            c[rx(k)] = -1
        res = None
        for slack in (1e-6, 1e-5, 1e-4):  # lock slack only guards HiGHS presolve tolerance
            if b_ub and len(b_ub) > len(act) and res is not None:
                b_ub[-1] = b_ub[-1] - slack
            res = linprog(c, A_ub=np.array(A_ub), b_ub=np.array(b_ub), A_eq=np.array(A_eq),
                          b_eq=np.array(b_eq), bounds=bounds, method="highs")
            if res.success:
                break
        if not res.success:
            raise RuntimeError(f"reference LP failed: {res.message}")
        opt = -res.fun
        result[p] = opt
        row = np.zeros(nv)
        for k in idx:
            row[rx(k)] = -1
        A_ub.append(row)
        b_ub.append(-opt + 1e-6 * max(1.0, opt))
    return result


# ── Invariant checking from API responses ─────────────────────────────

def check_state(c, ctx, failures, tag):
    topo = c.get("/topology").json()
    dem = c.get("/demands").json()["demands"]
    met = c.get("/metrics").json()
    nodes = {n["id"] for n in topo["nodes"]}
    links = topo["links"]
    lmap = {l["id"]: l for l in links}
    pair = {frozenset((l["source"], l["target"])): l for l in links}
    adj = active_adj(links)
    loads = defaultdict(float)

    def bad(msg):
        failures.append(f"{tag}: {msg}")

    for l in links:
        for f in ("capacity", "latency", "allocated_load", "utilization"):
            if not isinstance(l[f], (int, float)) or not math.isfinite(l[f]) or l[f] < 0:
                bad(f"link {l['id']} {f}={l[f]}")
    for d in dem:
        a, bw, un = d["allocated_bandwidth"], d["bandwidth"], d["unserved_bandwidth"]
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x >= -1e-9 for x in (a, bw, un)):
            bad(f"demand {d['id']} nonfinite/negative numbers")
            continue
        if a > bw + TOL:
            bad(f"demand {d['id']} allocated {a} > requested {bw}")
        if d["status"] != "pending" and abs(a + un - bw) > 0.02:
            bad(f"demand {d['id']} allocated+unserved != requested")
        if d["source"] == d["destination"]:
            continue
        tsum = 0.0
        for t in d.get("tunnels") or []:
            path, rate = t["node_path"], t["bandwidth"]
            lat = sum(pair[frozenset((u, v))]["latency"] for u, v in zip(path, path[1:])
                      if frozenset((u, v)) in pair)
            if d.get("max_latency") and rate > 1e-9 and lat > d["max_latency"] + 1e-6:
                bad(f"demand {d['id']} tunnel latency {lat} exceeds limit {d['max_latency']}")
            if not math.isfinite(rate) or rate < -1e-9:
                bad(f"demand {d['id']} tunnel rate {rate}")
            if path[0] != d["source"] or path[-1] != d["destination"] or len(set(path)) != len(path):
                bad(f"demand {d['id']} tunnel {path} not a simple src->dst path")
            for u, v in zip(path, path[1:]):
                l = pair.get(frozenset((u, v)))
                if l is None:
                    bad(f"demand {d['id']} tunnel uses nonexistent edge {u}-{v}")
                    continue
                if l["status"] != "active" and rate > 1e-9:
                    bad(f"demand {d['id']} carries {rate} on unavailable link {l['id']} ({l['status']})")
                loads[l["id"]] += rate
            tsum += rate
        if d["status"] != "pending" and abs(tsum - a) > 0.01:
            bad(f"demand {d['id']} tunnel sum {tsum} != allocated {a}")
        st = d["status"]
        if st == "fully_served" and a < bw - 0.01:
            bad(f"demand {d['id']} fully_served but {a}/{bw}")
        if st == "unreachable" and reachable(adj, d["source"], d["destination"]):
            bad(f"demand {d['id']} marked unreachable but a live path exists")
        if st in ("fully_served", "partially_served") and not reachable(adj, d["source"], d["destination"]):
            bad(f"demand {d['id']} served without a live path")
    for lid, l in lmap.items():
        if loads[lid] > l["capacity"] + TOL:
            bad(f"link {lid} load {loads[lid]:.4f} exceeds capacity {l['capacity']}")
        if l["status"] == "active" and abs(loads[lid] - l["allocated_load"]) > 0.01:
            bad(f"link {lid} reported load {l['allocated_load']} != tunnel sum {loads[lid]:.4f}")
        if l["status"] != "active" and l["allocated_load"] > 1e-6:
            bad(f"unavailable link {lid} reports load {l['allocated_load']}")
    pp = met.get("per_priority", {})
    for key, row in pp.items():
        want = sum(d["bandwidth"] for d in dem if f"P{PRIO[d['category']]}" == key)
        if abs(row["requested"] - want) > 0.01:
            bad(f"per_priority {key} requested {row['requested']} != {want}")
    evs = c.get("/events?limit=1000").json()
    if any(a["step"] < b["step"] for a, b in zip(evs, evs[1:])):
        bad("event log (newest first) is not ordered by step")
    tot = sum(d["allocated_bandwidth"] for d in dem if d["status"] != "pending")
    if abs(tot - met["total_delivered"]) > 0.05:
        bad(f"metrics total_delivered {met['total_delivered']} != demand sum {tot:.3f}")
    # Reference bound: no allocation may beat the arc-LP lexicographic optimum at the first differing class.
    live = [d for d in dem if d["status"] != "pending"]
    if live and len(live) == len(dem) and not any(d.get("max_latency") for d in dem):
        ref = arc_lp_optimum(nodes, links, [{**d} for d in dem])
        got = defaultdict(float)
        for d in dem:
            if d["source"] != d["destination"]:
                got[PRIO[d["category"]]] += d["allocated_bandwidth"]
        for p in range(1, 6):
            if got[p] > ref[p] + 0.01:
                bad(f"P{p} delivered {got[p]:.3f} exceeds independent optimum {ref[p]:.3f}")
                break
            if got[p] < ref[p] - 0.01:
                break
        ctx["last_ref"] = ref
    check_plan(c, links, dem, failures, tag)


def check_plan(c, links, dem, failures, tag):
    plan = c.get("/protection/plan").json()
    if not plan.get("active"):
        return
    dmap = {d["id"]: d for d in dem}
    for r in plan["critical_flows"]:
        g = r["guaranteed_mbps"]
        if not math.isfinite(g) or g < 0:
            failures.append(f"{tag}: plan guarantee {r['flow_id']}={g}")
        d = dmap.get(r["flow_id"])
        if d and g > d["bandwidth"] + TOL:
            failures.append(f"{tag}: guarantee {r['flow_id']} {g} > demand")
        if d and PRIO[d["category"]] > 2 and g > 0:
            failures.append(f"{tag}: P3-P5 flow {r['flow_id']} has a guarantee")
    if not plan.get("verified"):
        return
    if plan.get("in_force") and plan.get("current_scenario") != "no_failure" and plan.get("further_failures_covered"):
        failures.append(f"{tag}: plan claims further failures covered while in scenario {plan.get('current_scenario')}")
    if not plan.get("further_failures_covered"):
        check_in_force(plan, dmap, failures, tag)
        return
    # Physical-impossibility rule: the plan covers one more failure of any usable link, so if
    # such a failure disconnects a critical flow (or it is already disconnected) its guarantee must be 0.
    base = [l for l in links if l["status"] == "active"]
    for r in plan["critical_flows"]:
        d = dmap.get(r["flow_id"])
        if not d or PRIO[d["category"]] > 2 or r["guaranteed_mbps"] <= 0 or d["source"] == d["destination"]:
            continue
        if not reachable(active_adj(links), d["source"], d["destination"]):
            failures.append(f"{tag}: disconnected flow {d['id']} has guarantee {r['guaranteed_mbps']}")
            continue
        for l in base:
            trial = [dict(x, status=("failed" if x["id"] == l["id"] else x["status"])) for x in links]
            if not reachable(active_adj(trial), d["source"], d["destination"]):
                failures.append(f"{tag}: flow {d['id']} guaranteed {r['guaranteed_mbps']} but failing "
                                f"{l['id']} disconnects it")
                break
    check_in_force(plan, dmap, failures, tag)


def check_in_force(plan, dmap, failures, tag):
    """When the plan says it is in force, the live allocation must honour every guarantee."""
    if plan.get("in_force"):
        for r in plan["critical_flows"]:
            d = dmap.get(r["flow_id"])
            if d and r.get("planned", True) and d["status"] != "pending" \
                    and d["allocated_bandwidth"] < r["guaranteed_mbps"] - 0.01:
                failures.append(f"{tag}: plan in force but {d['id']} receives {d['allocated_bandwidth']} "
                                f"< guarantee {r['guaranteed_mbps']}")


# ── Random operations ─────────────────────────────────────────────────

def run_sequence(seed, n_ops, stats):
    rng = random.Random(seed)
    st = SimulationState()
    main.state = st
    c = TestClient(main.app, raise_server_exceptions=False)
    failures = []
    nodes, links, demands = random_scenario(rng)
    r = c.post("/topology/import", json={
        "nodes": [{"id": n, "name": n, "x": 0, "y": 0} for n in nodes],
        "links": links,
        "demands": [{**d, "priority": PRIO[d["category"]]} for d in demands]})
    if r.status_code != 200:
        return [f"seed {seed}: import failed {r.status_code} {r.text}"]
    ctx = {}
    last_step = 0
    for i in range(n_ops):
        topo = c.get("/topology").json()
        lids = [l["id"] for l in topo["links"]]
        dids = [d["id"] for d in c.get("/demands").json()["demands"]]
        nids = [n["id"] for n in topo["nodes"]]
        op = rng.choices(
            ["sim", "fail", "restore", "batch", "whatif", "compare", "replan", "add_demand",
             "edit_demand", "del_demand", "add_link", "del_link", "edit_link", "move_node", "bad_input",
             "predict", "raw_bad"],
            weights=[16, 14, 10, 3, 6, 4, 4, 5, 4, 3, 3, 2, 3, 4, 4, 6, 2])[0]
        tag = f"seed {seed} op {i} {op}"
        stats[op] = stats.get(op, 0) + 1
        resp = None
        if op == "sim":
            resp = c.post(f"/simulate?algorithm={rng.choice(['pacar', 'lp', 'reactive_lp', 'baseline'])}")
        elif op == "fail" and lids:
            resp = c.post(f"/links/{rng.choice(lids)}/fail")
        elif op == "restore" and lids:
            resp = c.post(f"/links/{rng.choice(lids)}/restore")
        elif op == "batch" and len(lids) >= 2:
            resp = c.post("/failures/batch", json={"link_ids": rng.sample(lids, 2)})
        elif op == "whatif" and lids:
            before = c.get("/topology").json(), c.get("/demands").json()
            req = rng.sample(lids, min(len(lids), rng.randint(1, 2)))
            resp = c.post("/analyze/what-if", json={"link_ids": req})
            after = c.get("/topology").json(), c.get("/demands").json()
            strip = lambda x: json.dumps(x, sort_keys=True)
            if strip(before[0]["links"]) != strip(after[0]["links"]) or strip(before[1]["demands"]) != strip(after[1]["demands"]):
                failures.append(f"{tag}: What-If mutated live state")
            if resp.status_code == 200:
                w = resp.json()
                for k in ("total_delivered_before", "total_delivered_after"):
                    if not math.isfinite(w[k]) or w[k] < 0:
                        failures.append(f"{tag}: what-if {k}={w[k]}")
        elif op == "predict" and lids:
            active_ids = [l["id"] for l in topo["links"] if l["status"] == "active"]
            pending = any(d["status"] == "pending" for d in c.get("/demands").json()["demands"])
            if active_ids and not pending:
                lid = rng.choice(active_ids)
                w = c.post("/analyze/what-if", json={"link_ids": [lid]})
                resp = c.post(f"/links/{lid}/fail")
                if w.status_code == 200 and resp.status_code == 200:
                    actual = c.get("/metrics").json()["total_delivered"]
                    ctx.setdefault("predict", []).append(abs(w.json()["total_delivered_after"] - actual) <= 0.05)
                    if abs(w.json()["total_delivered_after"] - actual) > 0.05:
                        failures.append(f"{tag}: what-if predicted {w.json()['total_delivered_after']} after "
                                        f"failing {lid}, controller delivered {actual}")
        elif op == "raw_bad":
            snap = json.dumps([c.get("/topology").json()["links"], c.get("/demands").json()["demands"]], sort_keys=True)
            body = rng.choice(['{"id": "Q", "source": "N0", "target": "N1", "capacity": NaN, "latency": 1}',
                               '{"id": "Q", "source": "N0", "target": "N1", "capacity": Infinity, "latency": 1}',
                               '{"id": "Q", "source": "N0", "target": "N1", "capacity": 5, ',
                               '[1, 2, 3]', '{"id": "Q", "source": "N0", "target": "N1", "capacity": "big", "latency": 1}'])
            r2 = c.post("/links", content=body, headers={"Content-Type": "application/json"})
            if r2.status_code not in (400, 422):
                failures.append(f"{tag}: malformed body {body!r} -> {r2.status_code}")
            if json.dumps([c.get("/topology").json()["links"], c.get("/demands").json()["demands"]], sort_keys=True) != snap:
                failures.append(f"{tag}: malformed request mutated state")
        elif op == "compare":
            before = json.dumps(c.get("/demands").json(), sort_keys=True)
            resp = c.post("/compare")
            if json.dumps(c.get("/demands").json(), sort_keys=True) != before:
                failures.append(f"{tag}: compare mutated live state")
            if resp.status_code == 200:
                for alg, m in resp.json().items():
                    if m["total_delivered"] > m["total_requested"] + TOL:
                        failures.append(f"{tag}: compare {alg} delivered > requested")
                    for lu in m["link_utilizations"]:
                        if lu["allocated_load"] > lu["capacity"] + TOL:
                            failures.append(f"{tag}: compare {alg} link {lu['id']} over capacity")
        elif op == "replan":
            resp = c.post("/protection/replan")
        elif op == "add_demand" and len(nids) >= 2:
            a, b = rng.sample(nids, 2)
            resp = c.post("/demands", json={"id": f"X{seed}_{i}", "source": a, "destination": b,
                                            "bandwidth": float(rng.choice([2, 10, 30])),
                                            "category": rng.choice(CATS)})
        elif op == "edit_demand" and dids:
            resp = c.put(f"/demands/{rng.choice(dids)}", json={"bandwidth": float(rng.choice([1, 8, 35])),
                                                                "category": rng.choice(CATS)})
        elif op == "del_demand" and dids:
            resp = c.delete(f"/demands/{rng.choice(dids)}")
        elif op == "add_link" and len(nids) >= 2:
            a, b = rng.sample(nids, 2)
            resp = c.post("/links", json={"id": f"LX{seed}_{i}", "source": a, "target": b,
                                          "capacity": float(rng.choice([5, 30, 80])), "latency": 2.0})
        elif op == "del_link" and lids:
            resp = c.delete(f"/links/{rng.choice(lids)}")
        elif op == "edit_link" and lids:
            resp = c.put(f"/links/{rng.choice(lids)}", json={"capacity": float(rng.choice([1, 15, 60])),
                                                              "latency": float(rng.randint(1, 9))})
        elif op == "move_node" and nids:
            resp = c.put(f"/nodes/{rng.choice(nids)}", json={"x": rng.randint(0, 500), "y": rng.randint(0, 500)})
        elif op == "bad_input":
            snapshot = json.dumps([c.get("/topology").json()["links"], c.get("/demands").json()["demands"]], sort_keys=True)
            bad_req = rng.choice([
                ("post", "/links", {"id": "BAD", "source": "N0", "target": "N0", "capacity": 5, "latency": 1}),
                ("post", "/links", {"id": "BAD", "source": "N0", "target": "N1", "capacity": -5, "latency": 1}),
                ("post", "/demands", {"id": "BAD", "source": "N0", "destination": "NOPE", "bandwidth": 5}),
                ("post", "/demands", {"id": "BAD", "source": "N0", "destination": "N1", "bandwidth": 0}),
                ("post", "/analyze/what-if", {"link_ids": ["NOPE"]}),
                ("post", "/links/NOPE/fail", None),
                ("put", f"/links/{lids[0] if lids else 'L1'}", {"capacity": -1}),
                ("post", "/simulate?algorithm=bogus", None),
            ])
            m, path, body = bad_req
            resp = getattr(c, m)(path, json=body) if body is not None else getattr(c, m)(path)
            if resp.status_code < 400:
                failures.append(f"{tag}: invalid request {path} {body} accepted ({resp.status_code})")
            after = json.dumps([c.get("/topology").json()["links"], c.get("/demands").json()["demands"]], sort_keys=True)
            if after != snapshot:
                failures.append(f"{tag}: rejected request mutated state")
            resp = None
        if resp is not None:
            if resp.status_code >= 500:
                failures.append(f"{tag}: HTTP {resp.status_code} {resp.text[:300]}")
                continue
            if resp.status_code == 200 and isinstance(resp.json(), dict) and "step" in resp.json():
                step = resp.json()["step"]
                if step < last_step:
                    failures.append(f"{tag}: step went backwards {last_step}->{step}")
                last_step = step
                evs = c.get("/events?limit=1000").json()
                if any(e["step"] > step for e in evs):
                    failures.append(f"{tag}: event logged with a future step")
                if op in ("fail", "restore") and resp.json().get("changed") is not False:
                    if not evs or evs[0]["step"] != step:
                        failures.append(f"{tag}: newest event step {evs[0]['step'] if evs else None} "
                                        f"!= response step {step}")
                if op in ("fail", "restore") and resp.json().get("changed") is False and evs and evs[0]["step"] > step:
                    failures.append(f"{tag}: no-op logged an event")
                v = resp.json().get("violations")
                if v:
                    failures.append(f"{tag}: controller reported violations {v[:3]}")
        try:
            check_state(c, ctx, failures, tag)
        except Exception as exc:
            failures.append(f"{tag}: checker exception {type(exc).__name__}: {exc}")
        if len(failures) > 5:
            break
    return failures


def lp_optimality_trials(n, first_seed, results):
    """Reactive LP must reach the independent lexicographic optimum whenever K covers all paths."""
    checked = exact = 0
    fails = []
    for seed in range(first_seed, first_seed + n):
        rng = random.Random(10_000 + seed)
        nodes, links, demands = random_scenario(rng)
        st = SimulationState()
        main.state = st
        c = TestClient(main.app)
        c.post("/topology/import", json={"nodes": [{"id": x, "name": x} for x in nodes], "links": links,
                                         "demands": [{**d, "priority": PRIO[d["category"]]} for d in demands]})
        if rng.random() < 0.5 and links:
            c.post(f"/links/{rng.choice(links)['id']}/fail")
        c.post("/simulate?algorithm=lp")
        topo = c.get("/topology").json()
        dem = c.get("/demands").json()["demands"]
        routed = [d for d in dem if d["source"] != d["destination"]]
        all_paths = {d["id"]: simple_paths(topo["links"], d["source"], d["destination"]) for d in routed}
        feas = {d["id"]: [pth for pth in all_paths[d["id"]]
                          if not d.get("max_latency") or path_latency(topo["links"], pth) <= d["max_latency"] + 1e-9]
                for d in routed}
        plan = c.get("/protection/plan").json()
        floors = None
        if plan.get("in_force"):
            floors = {r["flow_id"]: r["guaranteed_mbps"] for r in plan["critical_flows"] if r.get("planned")}
        ref = path_lp_optimum(topo["links"], routed, feas, floors)
        if not floors and not any(d.get("max_latency") for d in routed):
            arc = arc_lp_optimum(nodes, topo["links"], dem)
            for p in range(1, 6):  # independent cross-check of the path reference itself
                if abs(arc[p] - ref[p]) > 0.01:
                    fails.append(f"lp seed {seed}: reference mismatch arc {arc[p]} vs path {ref[p]} (P{p})")
                    break
                # (arc and path optima can only differ through a reference bug when all paths are enumerated)
        got = defaultdict(float)
        for d in routed:
            got[PRIO[d["category"]]] += d["allocated_bandwidth"]
        complete = all(len(all_paths[d["id"]]) <= K for d in routed)
        checked += 1
        if complete:
            exact += 1
            for p in range(1, 6):
                if abs(got[p] - ref[p]) > 0.01:
                    fails.append(f"lp seed {seed}: P{p} LP {got[p]:.3f} != optimum {ref[p]:.3f} "
                                 f"(all paths enumerated)")
                    break
    results["lp_optimality"] = {"instances": checked, "exact_comparisons": exact, "failures": fails}
    return fails


def planner_trials(n, first_seed, results):
    """Independent re-derivation of the protection LP over all simple paths."""
    checked = exact = 0
    fails = []
    for seed in range(first_seed, first_seed + n):
        rng = random.Random(20_000 + seed)
        nodes, links, demands = random_scenario(rng)
        for d in demands:
            if rng.random() < 0.6:
                d["category"] = rng.choice(["emergency", "essential"])
        st = SimulationState()
        main.state = st
        c = TestClient(main.app)
        c.post("/topology/import", json={"nodes": [{"id": x, "name": x} for x in nodes], "links": links,
                                         "demands": [{**d, "priority": PRIO[d["category"]]} for d in demands]})
        if rng.random() < 0.4 and links:
            c.post(f"/links/{rng.choice(links)['id']}/fail")
        c.post("/simulate?algorithm=pacar")
        plan = c.get("/protection/plan").json()
        if not plan.get("active"):
            continue
        topo = c.get("/topology").json()
        all_crit = [d for d in c.get("/demands").json()["demands"] if PRIO[d["category"]] <= 2]
        local_g = {1: 0.0, 2: 0.0}
        for d in all_crit:
            if d["source"] == d["destination"]:
                local_g[PRIO[d["category"]]] += d["bandwidth"]  # local traffic needs no link
        crit = [d for d in all_crit if d["source"] != d["destination"]]
        if not all_crit:
            continue
        active = [l for l in topo["links"] if l["status"] == "active"]
        raw = {d["id"]: simple_paths(topo["links"], d["source"], d["destination"]) for d in crit}
        complete = all(len(p) <= K for p in raw.values())
        paths = {d["id"]: [pth for pth in raw[d["id"]]
                           if not d.get("max_latency") or path_latency(topo["links"], pth) <= d["max_latency"] + 1e-9]
                 for d in crit}
        ref = independent_protection_lp(crit, active, paths) if crit else {1: 0.0, 2: 0.0}
        ref = {p: ref[p] + local_g[p] for p in (1, 2)}
        got = {1: 0.0, 2: 0.0}
        for r in plan["critical_flows"]:
            if r["priority"] in (1, 2):
                got[r["priority"]] += r["guaranteed_mbps"]
        checked += 1
        if not plan.get("verified"):
            fails.append(f"plan seed {seed}: plan not verified: {plan.get('verification_failures')}")
            continue
        for p in (1, 2):
            if got[p] > ref[p] + 0.02:
                fails.append(f"plan seed {seed}: P{p} guarantee {got[p]:.3f} exceeds independent optimum {ref[p]:.3f}")
                break
            if complete and got[p] < ref[p] - 0.02:
                fails.append(f"plan seed {seed}: P{p} guarantee {got[p]:.3f} below independent optimum {ref[p]:.3f}")
                break
            if abs(got[p] - ref[p]) > 0.02:
                break
        if complete:
            exact += 1
    results["planner_optimality"] = {"instances": checked, "exact_comparisons": exact, "failures": fails}
    return fails


def independent_protection_lp(crit, active, paths):
    """max lexicographic (sum P1 b, sum P2 b) s.t. shared reservations survive every single failure."""
    var = []
    for d in crit:
        for i, _ in enumerate(paths[d["id"]]):
            var.append(("a", d["id"], i))
        var.append(("b", d["id"], None))
    idx = {v: i for i, v in enumerate(var)}
    nv = len(var)
    A, b = [], []
    for l in active:
        row = np.zeros(nv)
        for d in crit:
            for i, p in enumerate(paths[d["id"]]):
                if l["id"] in p:
                    row[idx[("a", d["id"], i)]] = 1
        A.append(row)
        b.append(l["capacity"])
    for d in crit:
        for q in [None] + [l["id"] for l in active]:
            row = np.zeros(nv)
            row[idx[("b", d["id"], None)]] = 1
            for i, p in enumerate(paths[d["id"]]):
                if q not in p:
                    row[idx[("a", d["id"], i)]] = -1
            A.append(row)
            b.append(0.0)
    bounds = [(0, None) if v[0] == "a" else (0, next(x["bandwidth"] for x in crit if x["id"] == v[1])) for v in var]
    out = {}
    for p in (1, 2):
        cvec = np.zeros(nv)
        ids = [d["id"] for d in crit if PRIO[d["category"]] == p]
        for fid in ids:
            cvec[idx[("b", fid, None)]] = -1
        if not ids:
            out[p] = 0.0
            continue
        res = linprog(cvec, A_ub=np.array(A), b_ub=np.array(b), bounds=bounds, method="highs")
        out[p] = -res.fun if res.success else float("nan")
        row = np.zeros(nv)
        for fid in ids:
            row[idx[("b", fid, None)]] = -1
        A.append(row)
        b.append(-out[p] + 1e-7)
    return out


if __name__ == "__main__":
    n_seq = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    n_ops = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    t0 = time.time()
    stats, all_fail = {}, []
    results = {}
    for seed in range(first, first + n_seq):
        f = run_sequence(seed, n_ops, stats)
        all_fail.extend(f)
    results["stateful"] = {"sequences": n_seq, "ops_per_sequence": n_ops, "first_seed": first,
                           "op_counts": stats, "failures": all_fail}
    lp_f = lp_optimality_trials(n_seq, first, results)
    pl_f = planner_trials(n_seq, first, results)
    results["elapsed_s"] = round(time.time() - t0, 1)
    total = all_fail + lp_f + pl_f
    results["total_failures"] = len(total)
    with open("qa/backend-property-results.json", "w") as fh:
        json.dump(results, fh, indent=2)
    for line in total[:60]:
        print("FAIL", line)
    print(f"\nstateful sequences={n_seq} ops={sum(stats.values())} failures={len(all_fail)}")
    print(f"LP optimality: {results['lp_optimality']['instances']} instances, "
          f"{results['lp_optimality']['exact_comparisons']} exact, failures={len(lp_f)}")
    print(f"Planner optimality: {results['planner_optimality']['instances']} instances, "
          f"{results['planner_optimality']['exact_comparisons']} exact, failures={len(pl_f)}")
    print(f"elapsed {results['elapsed_s']}s")
    sys.exit(1 if total else 0)
