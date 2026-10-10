"""Algorithm Explorer: trace fidelity, narrative accuracy, isolation and API tests.

The explorer must (1) never change what the algorithms compute, (2) describe only
what the real run recorded, and (3) never mutate the live network.
"""
from __future__ import annotations

import copy
import json
import math
import random
import re
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import Demand, Link, Node, TrafficCategory, CATEGORY_TO_PRIORITY
from app.state import SimulationState
from app.simulation import engine
from app.routing.baseline import run_baseline
from app.routing.pacar import run_pacar, path_score_breakdown, compute_path_score
from app.routing.reactive_lp import solve_reactive_multicommodity_lp
from app.routing.protection_planner import plan_proactive_protection
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.reflex import run_reflex_recovery
from app.routing.install_policy import evaluate_install_policy
from app.explorer import service
from app.explorer.trace import TraceRecorder, json_safe
from app.explorer.signatures import outcome_signature, scenario_fingerprint, diff_signatures
from app.scenarios.presets import SCENARIOS, load_scenario

CATS = ["emergency", "essential", "academic", "general", "background"]
DEMO_EXTRA = {"id": "P1_DEMO", "source": "A", "destination": "E", "bandwidth": 10, "category": "emergency"}


# ── helpers ───────────────────────────────────────────────────────────

def random_network(seed: int):
    rng = random.Random(seed)
    n = rng.randint(3, 8)
    nodes = [f"N{i}" for i in range(n)]
    order = nodes[:]
    rng.shuffle(order)
    edges = set()
    connected = rng.random() < 0.85
    for i in range(1, n):
        if not connected and i == n - 1:
            break
        edges.add(tuple(sorted((order[i], order[rng.randrange(i)]))))
    for _ in range(rng.randint(0, n + 1)):
        a, b = rng.sample(nodes, 2)
        edges.add(tuple(sorted((a, b))))
    links = [Link(id=f"L{i + 1}", source=a, target=b, capacity=float(rng.choice([1, 5, 10, 20, 40, 50, 100])),
                  latency=float(rng.randint(1, 9))) for i, (a, b) in enumerate(sorted(edges))]
    demands = []
    for i in range(rng.randint(1, 7)):
        a = rng.choice(nodes)
        b = rng.choice(nodes) if rng.random() < 0.1 else rng.choice([x for x in nodes if x != a])
        cat = TrafficCategory(rng.choice(CATS))
        demands.append(Demand(id=f"D{i + 1}", source=a, destination=b,
                              bandwidth=float(rng.choice([1, 3, 5, 10, 15, 25, 40, 60])), category=cat,
                              priority=CATEGORY_TO_PRIORITY[cat],
                              max_latency=float(rng.randint(3, 25)) if rng.random() < 0.15 else None))
    return [Node(id=x, name=x) for x in nodes], links, demands


def loaded_state(nodes, links, demands) -> SimulationState:
    s = SimulationState()
    s.load_topology(nodes, links, demands)
    return s


def preset_state(key: str, extra: list | None = None) -> SimulationState:
    sc = load_scenario(key)
    demands = list(sc.get("demands", []))
    for e in extra or []:
        cat = TrafficCategory(e["category"])
        demands.append(Demand(id=e["id"], source=e["source"], destination=e["destination"],
                              bandwidth=e["bandwidth"], category=cat, priority=CATEGORY_TO_PRIORITY[cat]))
    s = loaded_state(sc["nodes"], sc["links"], demands)
    for lid in sc.get("fail_links", []):
        s.fail_link(lid)
    return s


def demo_state() -> SimulationState:
    return preset_state("congested_shortest", [DEMO_EXTRA])


def strip_timing(obj):
    if isinstance(obj, dict):
        return {k: strip_timing(v) for k, v in obj.items()
                if not any(t in str(k) for t in ("time", "runtime", "_ms"))}
    if isinstance(obj, list):
        return [strip_timing(v) for v in obj]
    return obj


def live_vars(state: SimulationState) -> dict:
    return copy.deepcopy({k: v for k, v in vars(state).items() if k != "_lock"})


def events(rec: TraceRecorder, kind: str) -> list[dict]:
    return [e["data"] for e in rec.events if e["kind"] == kind]


@pytest.fixture
def client(monkeypatch):
    isolated = SimulationState()
    monkeypatch.setattr(main, "state", isolated)
    with TestClient(main.app) as c:
        yield c, isolated


def load_demo(c):
    assert c.post("/topology/generate", json={"preset": "congested_shortest"}).status_code == 200
    assert c.post("/demands", json=DEMO_EXTRA).status_code == 200
    assert c.post("/simulate?algorithm=pacar").status_code == 200


def new_session(c) -> str:
    r = c.post("/explorer/sessions")
    assert r.status_code == 200
    return r.json()["session_id"]


def trace(c, sid, algorithm, failed_link=None):
    r = c.post(f"/explorer/sessions/{sid}/trace", json={"algorithm": algorithm, "failed_link": failed_link})
    assert r.status_code == 200, r.text
    data = r.json()
    json.dumps(data, allow_nan=False)  # strict JSON
    return data


# ── 1. Recorder ───────────────────────────────────────────────────────

def test_json_safe_converts_non_finite_numpy_sets_and_enums():
    out = json_safe({"a": float("inf"), "b": float("nan"), "c": np.float64(1.5), "d": np.int64(3),
                     "e": {3, 1, 2}, "f": TrafficCategory.EMERGENCY, "g": (1, 2), 4: np.array([1.0, 2.0])})
    assert out == {"a": None, "b": None, "c": 1.5, "d": 3, "e": [1, 2, 3], "f": "emergency",
                   "g": [1, 2], "4": [1.0, 2.0]}
    json.dumps(out, allow_nan=False)


def test_emit_copies_values_and_never_raises():
    rec = TraceRecorder()
    residual = {"L1": 5.0}
    rec.emit("x.state", residual=residual)
    residual["L1"] = 0.0
    assert rec.events[0]["data"]["residual"] == {"L1": 5.0}  # snapshot at emit time

    class Exploding:
        def __str__(self):
            raise RuntimeError("boom")
    rec.emit("x.bad", obj=Exploding())  # must not raise
    assert rec.errors and "x.bad" in rec.errors[0]
    small = TraceRecorder(max_events=2)
    for i in range(5):
        small.emit("x.n", i=i)
    assert len(small.events) == 2 and small.truncated


def test_compute_path_score_delegates_to_breakdown():
    s = demo_state()
    from app.routing.baseline import build_graph
    G = build_graph(s)
    residual = {lid: l.capacity for lid, l in s.links.items()}
    for path in (["A", "B", "E"], ["A", "C", "D", "E"]):
        b = path_score_breakdown(path, G, residual, s, max_latency=10.0, max_hops=10, max_capacity=200.0)
        assert compute_path_score(path, G, residual, s, max_latency=10.0, max_hops=10, max_capacity=200.0) == b["score"]
        recomputed = (0.30 * b["norm_latency"] + 0.35 * b["max_util"] + 0.10 * b["norm_hops"]
                      - 0.25 * b["norm_capacity"])
        assert recomputed == b["score"]
    assert path_score_breakdown(["A"], G, residual, s)["score"] == math.inf


# ── 2. Tracing never changes results ──────────────────────────────────

def _all_entry_points(base: SimulationState, traced: bool) -> dict:
    T = (lambda: TraceRecorder()) if traced else (lambda: None)
    out = {}
    for alg in ("pacar", "baseline", "reactive_lp"):
        s = base.snapshot()
        r = engine.simulate(s, alg, trace=T())
        out[f"sim_{alg}"] = [strip_timing(r), outcome_signature(s)]
    live = base.snapshot()
    engine.simulate(live, "pacar")
    snap = live.snapshot()
    out["baseline"] = [(d.id, d.allocated_bandwidth, d.route, d.status.value) for d in run_baseline(snap, trace=T())]
    out["pacar"] = [(d.id, d.allocated_bandwidth, d.route, d.status.value) for d in run_pacar(snap, trace=T())]
    out["lp"] = strip_timing(solve_reactive_multicommodity_lp(snap.demands, snap.links, snap.candidate_tunnels,
                                                              trace=T()))
    plan = plan_proactive_protection(snap.demands, snap.links, snap.candidate_tunnels, 1, 1, trace=T())
    chk = verify_protection_guarantees(plan, snap.demands, snap.links, snap.candidate_tunnels, trace=T())
    out["plan"] = [plan.per_flow_guaranteed, plan.per_tunnel_reservations, plan.solver_status, plan.verified,
                   chk.scenarios_passed, chk.failures]
    for lid in list(live.links):
        s = live.snapshot()
        r1 = engine.handle_link_failure(s, lid, trace=T())
        r2 = engine.handle_link_restore(s, lid, trace=T())
        out[f"fail_restore_{lid}"] = [strip_timing(r1), strip_timing(r2), outcome_signature(s)]
    return out


@pytest.mark.parametrize("key", sorted(SCENARIOS) + ["protected_demo"])
def test_trace_on_off_equivalence_presets(key):
    base = demo_state() if key == "protected_demo" else preset_state(key)
    assert _all_entry_points(base, traced=False) == _all_entry_points(base, traced=True)


def test_trace_on_off_equivalence_random_networks():
    for seed in range(40):
        base = loaded_state(*random_network(seed))
        assert _all_entry_points(base, traced=False) == _all_entry_points(base, traced=True), seed


# ── 3. Trace events correspond to the real execution ──────────────────

def test_baseline_trace_matches_results_and_residuals():
    for seed in list(range(25)) + ["demo"]:
        s = demo_state() if seed == "demo" else loaded_state(*random_network(seed))
        rec = TraceRecorder()
        results = {d.id: d for d in run_baseline(s, trace=rec)}
        init = events(rec, "baseline.init")[0]
        assert [o["id"] for o in init["order"]] == [d.id for d in sorted(s.demands.values(),
                                                                          key=lambda d: (d.priority.value, d.id))]
        paths = {e["demand"]: e for e in events(rec, "baseline.path")}
        residual = dict(init["residual"])
        for dec in events(rec, "baseline.decision"):
            r = results[dec["demand"]]
            assert dec["allocated"] == r.allocated_bandwidth
            p = paths[dec["demand"]]
            if r.allocated_bandwidth > 0:
                assert p["path"] == r.route
                assert dec["allocated"] == min(p["bandwidth"], p["bottleneck"])
            for lid in p["links"]:
                residual[lid] -= dec["allocated"]
            assert all(abs(residual[l] - dec["residual"][l]) < 1e-9 for l in residual)
            assert p["bottleneck"] == min(r_ for _, r_ in p["hop_residual"])
        done = events(rec, "baseline.done")[0]["results"]
        assert {x["id"]: x["allocated"] for x in done} == {k: v.allocated_bandwidth for k, v in results.items()}


def test_pacar_trace_matches_selection_scores_and_filling():
    for seed in list(range(25)) + ["demo"]:
        s = demo_state() if seed == "demo" else loaded_state(*random_network(seed))
        rec = TraceRecorder()
        results = {d.id: d for d in run_pacar(s, trace=rec)}
        fallback = {e["demand"]: e for e in events(rec, "pacar.fallback")}
        for c in events(rec, "pacar.candidates"):
            for cand in c["candidates"]:
                assert cand["terms"]["score"] == cand["score"]  # breakdown equals the decision's score
            eligible = [x for x in c["candidates"] if x["bottleneck"] > 0]
            if eligible:
                assert c["best_path"] == min(eligible, key=lambda x: x["score"])["path"]
            chosen = fallback[c["demand"]]["path"] if c["demand"] in fallback else c["best_path"]
            r = results[c["demand"]]
            if r.allocated_bandwidth > 0:
                assert r.route == chosen
        # progressive filling: deltas add up to each demand's allocation
        grown: dict[str, float] = {}
        for rd in events(rec, "pacar.fill_round"):
            for did in rd["eligible"]:
                grown[did] = grown.get(did, 0.0) + rd["delta"]
            assert rd["delta"] == min([sh[2] for sh in rd["shares"].values()] + list(rd["needs"].values()))
        for did, total in grown.items():
            r = results[did]
            assert abs(total - r.allocated_bandwidth) < 1e-6 or r.allocated_bandwidth == r.bandwidth
        done = {x["id"]: x["allocated"] for x in events(rec, "pacar.done")[0]["results"]}
        assert done == {k: v.allocated_bandwidth for k, v in results.items()}


def test_lp_trace_stage_optima_and_result_match_return_value():
    for seed in list(range(20)) + ["demo"]:
        s = demo_state() if seed == "demo" else loaded_state(*random_network(seed))
        engine.refresh_candidate_tunnels(s)
        rec = TraceRecorder()
        out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels, trace=rec)
        if not events(rec, "lp.result"):
            continue
        res = events(rec, "lp.result")[0]
        assert res["allocations"] == json_safe(out["allocations"])
        assert res["total_delivered"] == out["total_delivered"]
        inputs = events(rec, "lp.inputs")[0]
        assert inputs["num_vars"] == sum(len(f["variables"]) for f in inputs["flows"])
        for st in events(rec, "lp.stage"):
            if st["skipped"] or st["optimum"] is None:
                continue
            got = sum(sum(out["allocations"].get(fid, {}).values()) for fid in st["flows"])
            assert got >= st["optimum"] - 1e-5  # every class keeps its locked optimum
            assert out["delivered_by_priority"][f"P{st['priority']}"] == st["optimum"]


def test_protection_trace_matches_plan_and_checker():
    for key in ("protected_demo", "normal_campus", "congested_shortest", "equal_priority"):
        s = demo_state() if key == "protected_demo" else preset_state(key)
        engine.refresh_candidate_tunnels(s)
        rec = TraceRecorder()
        engine.update_simulation_protection_plan(s, trace=rec)
        plan = s.protection_plan
        if plan is None:
            assert events(rec, "engine.plan_skipped")
            continue
        res = events(rec, "plan.result")[0]
        assert res["guarantees"] == json_safe(plan.per_flow_guaranteed)
        assert res["reservations"] == json_safe(plan.per_tunnel_reservations)
        lookup = {(fid, t.id): t for fid, ts in s.candidate_tunnels.items() for t in ts}
        for fid, by_sc in events(rec, "plan.survival")[0]["matrix"].items():
            for sc, tids in by_sc.items():
                failed = {sc[5:]} if sc.startswith("fail_") else set()
                for tid in tids:
                    assert lookup[(fid, tid)].survives_failure(failed)
        checks = events(rec, "check.scenario")
        assert [c["scenario"] for c in checks] == plan.covered_scenarios
        verdict = events(rec, "check.result")[0]
        assert verdict["verified"] == plan.verified and verdict["passed"] == sum(c["passed"] for c in checks)


def test_reflex_trace_arithmetic_matches_allocation():
    s = demo_state()
    engine.simulate(s, "pacar")
    plan = s.protection_plan
    for lid in s.links:
        rec = TraceRecorder()
        out = run_reflex_recovery({lid}, plan, s.demands, s.links, s.candidate_tunnels,
                                  previous_allocations=s.active_allocations, trace=rec)
        assert out["success"]
        for cf in events(rec, "reflex.critical_flow"):
            assert cf["target"] == min(cf["requested"], cf["guaranteed"], cf["S"])
            assert abs(sum(cf["allocation"].values()) - (cf["target"] if cf["S"] > 1e-12 else 0)) < 1e-9
            for tid, a in cf["surviving"]:
                assert abs(cf["allocation"].get(tid, 0.0) - cf["target"] * a / cf["S"]) < 1e-9
        assert events(rec, "reflex.result")[0]["allocations"] == json_safe(out["allocations"])
        assert events(rec, "reflex.validation")[0]["valid"]


def test_policy_trace_records_the_deciding_rule():
    s = demo_state()
    engine.simulate(s, "pacar")
    snap = s.snapshot()
    lp = solve_reactive_multicommodity_lp(snap.demands, snap.links, snap.candidate_tunnels)
    rec = TraceRecorder()
    dec = evaluate_install_policy(lp["allocations"], snap.active_allocations, snap.demands, snap.links,
                                  snap.candidate_tunnels, snap.version, snap.version, trace=rec)
    ev = events(rec, "policy.decision")[0]
    assert ev["install"] == dec.should_install and ev["decision_type"] == dec.decision_type
    rec2 = TraceRecorder()
    dec2 = evaluate_install_policy(lp["allocations"], snap.active_allocations, snap.demands, snap.links,
                                   snap.candidate_tunnels, 1, 2, trace=rec2)
    assert not dec2.should_install and events(rec2, "policy.decision")[0]["rule"] == "stale_version"


# ── 4. Narrative accuracy on known scenarios ──────────────────────────

def test_narrative_pacar_demo_explains_real_scores(client):
    c, _ = client
    load_demo(c)
    t = trace(c, new_session(c), "pacar")
    assert t["verification"]["untraced_rerun_identical"] is True
    assert t["verification"]["matches_live_installed"] is True  # live routes came from PACAR
    p1 = next(st for st in t["steps"] if st["kind"] == "pacar.candidates" and st["title"].startswith("P1_DEMO"))
    assert "Selected A → C → D → E (score 0.080)" in p1["summary"]
    rows = p1["tables"][0]["rows"]
    assert rows[0][1] == "A → B → E" and rows[0][6] == "0.258"
    assert rows[1][1] == "A → C → D → E" and rows[1][7] == "✓ selected"
    why = next(x["text"] for x in p1["sections"] if x["label"] == "Why")
    assert "spare capacity -0.188" in why and "latency does not separate them" in why
    d3 = next(st for st in t["steps"] if st["kind"] == "pacar.candidates" and st["title"].startswith("D3"))
    assert "120 Mbps free" in next(x["text"] for x in d3["sections"] if x["label"] == "Why")
    assert t["result"]["metrics"]["total_delivered"] == 130.0
    # every step carries the fields the frontend relies on
    for st in t["steps"]:
        assert {"index", "group", "phase", "kind", "fidelity", "title", "summary", "sections", "tables", "viz"} <= set(st)
        assert st["fidelity"] in ("exact", "stage", "summary", "verification")
    assert "network" in t["steps"][0]


def test_narrative_recovery_demo_matches_runbook(client):
    c, _ = client
    load_demo(c)
    t = trace(c, new_session(c), "recovery", "L3")
    kinds = [st["kind"] for st in t["steps"]]
    assert kinds[0] == "engine.link_failed" and "engine.coverage" in kinds
    cov = t["steps"][kinds.index("engine.coverage")]
    assert cov["summary"] == "Covered → Reflex (solver-free)"
    flows = {st["title"].split(":")[0]: st for st in t["steps"] if st["kind"] == "reflex.critical_flow"}
    assert "= 10 Mbps" in flows["P1_DEMO"]["summary"] and "= 40 Mbps" in flows["D1"]["summary"]
    policy = t["steps"][kinds.index("policy.decision")]
    assert policy["summary"].startswith("KEEP CURRENT.")
    delivered = {d["id"]: d["delivered"] for d in t["result"]["demands"]}
    assert delivered == {"P1_DEMO": 10.0, "D1": 40.0, "D2": 0.0, "D3": 0.0}
    assert t["response"]["reflex_used"] is True
    # LP stages are labelled as stages; the explorer check as verification
    assert {st["fidelity"] for st in t["steps"] if st["kind"] == "lp.stage"} == {"stage"}
    assert {st["fidelity"] for st in t["steps"] if st["kind"] == "explorer.lp_check"} == {"verification"}


def test_narrative_protection_demo_and_zero_guarantee_campus(client):
    c, _ = client
    load_demo(c)
    t = trace(c, new_session(c), "protection")
    titles = [st["title"] for st in t["steps"]]
    assert "6 scenarios to survive" in titles and "Plan VERIFIED" in titles
    assert t["result"]["plan"]["guarantees"] == {"D1": 40.0, "P1_DEMO": 10.0, "D2": 0.0, "D3": 0.0}
    assert sum(1 for st in t["steps"] if st["kind"] == "check.scenario") == 6
    c.post("/topology/generate", json={"preset": "normal_campus"})
    c.post("/simulate?algorithm=pacar")
    t2 = trace(c, new_session(c), "protection")
    chosen = next(st for st in t2["steps"] if st["kind"] == "plan.guarantees_chosen")
    why = next(x["text"] for x in chosen["sections"] if x["label"] == "Why some are 0")
    assert "D1: in scenario “L8 fails” none of its tunnels survives" in why
    vacuous = [st for st in t2["steps"] if st["kind"] == "check.scenario"]
    assert all(st["summary"].startswith("PASS: no flow has a positive guarantee") for st in vacuous)


def test_lp_narrative_never_mentions_churn_or_invented_iterations(client):
    c, _ = client
    load_demo(c)
    t = trace(c, new_session(c), "reactive_lp")
    text = json.dumps([st for st in t["steps"] if st["group"] == "reactive_lp"]).lower()
    assert "churn" not in text and "beta" not in text
    for st in t["steps"]:
        if st["kind"] == "lp.stage":
            solver = next(x["text"] for x in st["sections"] if x["label"] == "Solver")
            assert "iterations reported" in solver
            assert any(x["label"] == "What is not shown" for x in st["sections"])


# ── 5. Edge cases and invalid inputs ──────────────────────────────────

def test_empty_network(client):
    c, _ = client
    sid = new_session(c)
    for alg in ("baseline", "pacar", "reactive_lp", "protection"):
        t = trace(c, sid, alg)
        assert t["steps"] and t["result"]["metrics"]["total_delivered"] == 0
    assert c.post(f"/explorer/sessions/{sid}/trace", json={"algorithm": "recovery"}).status_code == 400
    assert c.post(f"/explorer/sessions/{sid}/compare", json={}).status_code == 200


def test_disconnected_network_reports_unreachable(client):
    c, _ = client
    c.post("/nodes", json={"id": "A"})
    c.post("/nodes", json={"id": "B"})
    c.post("/nodes", json={"id": "C"})
    c.post("/links", json={"id": "L1", "source": "A", "target": "B", "capacity": 10, "latency": 1})
    c.post("/demands", json={"id": "D1", "source": "A", "destination": "C", "bandwidth": 5, "category": "emergency"})
    sid = new_session(c)
    for alg, kind in (("baseline", "baseline.unreachable"), ("pacar", "pacar.unreachable")):
        t = trace(c, sid, alg)
        st = next(s for s in t["steps"] if s["kind"] == kind)
        assert st["title"] == "D1: unreachable"
        assert t["result"]["demands"][0]["status"] == "unreachable"


def test_saturated_links_are_explained(client):
    c, _ = client
    c.post("/topology/generate", json={"preset": "equal_priority"})
    sid = new_session(c)
    t = trace(c, sid, "pacar")
    rounds = [st for st in t["steps"] if st["kind"] == "pacar.fill_round"]
    assert rounds and "L1" in rounds[0]["summary"]  # 100 Mbps shared by two 70 Mbps demands
    stop = next(st for st in t["steps"] if st["kind"] == "pacar.fill_stop")
    assert "L1" in stop["summary"]
    delivered = {d["id"]: d["delivered"] for d in t["result"]["demands"]}
    assert delivered == {"D1": 50.0, "D2": 50.0}
    b = trace(c, sid, "baseline")
    partial = [st for st in b["steps"] if st["kind"] == "baseline.decision" and "of 70 Mbps" in st["title"]]
    assert any("bottleneck" in st["summary"] for st in partial)


def test_invalid_requests(client):
    c, _ = client
    load_demo(c)
    sid = new_session(c)
    bad = [({"algorithm": "dijkstra2"}, 400), ({"algorithm": "pacar", "failed_link": "NOPE"}, 400),
           ({"algorithm": "recovery"}, 400), ({}, 422)]
    for body, code in bad:
        assert c.post(f"/explorer/sessions/{sid}/trace", json=body).status_code == code, body
    c.post("/links/L3/fail")
    sid2 = new_session(c)
    assert c.post(f"/explorer/sessions/{sid2}/trace", json={"algorithm": "recovery", "failed_link": "L3"}).status_code == 400
    assert c.post(f"/explorer/sessions/{sid2}/trace", json={"algorithm": "pacar", "failed_link": "L3"}).status_code == 400
    assert c.post("/explorer/sessions/does-not-exist/trace", json={"algorithm": "pacar"}).status_code == 404
    assert c.get("/explorer/sessions/does-not-exist").status_code == 404


# ── 6. Isolation, determinism, fairness, concurrency ──────────────────

def test_explorer_never_mutates_live_state(client):
    c, live = client
    load_demo(c)
    before = live_vars(live)
    fp = scenario_fingerprint(live)
    sid = new_session(c)
    for alg, fl in (("baseline", None), ("pacar", None), ("reactive_lp", None), ("protection", None),
                    ("recovery", "L3"), ("pacar", "L1"), ("protection", "L3")):
        trace(c, sid, alg, fl)
    c.post(f"/explorer/sessions/{sid}/compare", json={})
    c.post(f"/explorer/sessions/{sid}/compare", json={"failed_link": "L3"})
    c.get("/explorer/live")
    c.post("/explorer/live-action/trace")
    assert live_vars(live) == before
    assert scenario_fingerprint(live) == fp


def test_session_snapshot_is_frozen(client):
    c, live = client
    load_demo(c)
    sid = new_session(c)
    snap_fp = c.get(f"/explorer/sessions/{sid}").json()["fingerprint"]
    c.post("/links/L3/fail")  # live network changes afterwards
    assert c.get("/explorer/live").json()["fingerprint"] != snap_fp
    t = trace(c, sid, "pacar")
    assert t["fingerprint"] == snap_fp
    assert t["result"]["metrics"]["total_delivered"] == 130.0  # computed on the pre-failure snapshot
    assert {l["id"]: l["status"] for l in t["result"]["links"]}["L3"] == "active"
    # running a trace never mutates the stored snapshot
    sess = service.get_session(sid)
    assert scenario_fingerprint(sess.snapshot) == snap_fp


def test_traces_are_deterministic_across_sessions(client):
    c, _ = client
    load_demo(c)
    a, b = new_session(c), new_session(c)
    for alg, fl in (("pacar", None), ("baseline", None), ("reactive_lp", None), ("recovery", "L3"), ("protection", None)):
        ta, tb = trace(c, a, alg, fl), trace(c, b, alg, fl)
        # Only wall-clock measurements ("took 2.4 ms", "0.11 ms of computation") may differ.
        measured = lambda steps: re.sub(r"took [0-9.]+ ms|[0-9.]+ ms of computation", "<t>",
                                        json.dumps(strip_timing(steps)))
        assert measured(ta["steps"]) == measured(tb["steps"]), alg
        assert ta["result"] == tb["result"]


def test_comparison_uses_identical_copies_of_one_snapshot(client):
    c, live = client
    load_demo(c)
    sid = new_session(c)
    cmp = c.post(f"/explorer/sessions/{sid}/compare", json={}).json()
    snap = service.get_session(sid).snapshot
    for row in cmp["rows"]:
        individual = trace(c, sid, row["algorithm"])
        assert row["metrics"] == individual["result"]["metrics"]
        assert row["verified_identical"] is True
        fresh = snap.snapshot()
        engine.simulate(fresh, row["algorithm"])
        fresh_summary = json.loads(json.dumps(json_safe(service.summarize_result(fresh))))
        assert fresh_summary["demands"] == individual["result"]["demands"]  # same routes, rates, statuses
        assert fresh_summary["metrics"] == row["metrics"]
    by = {r["algorithm"]: r["metrics"]["total_delivered"] for r in cmp["rows"]}
    assert by == {"baseline": 160.0, "pacar": 130.0, "reactive_lp": 160.0}


def test_concurrent_explorer_requests_and_live_actions(client):
    c, live = client
    load_demo(c)
    sid = new_session(c)
    errors: list[str] = []

    def explorer_worker(i):
        try:
            for alg, fl in (("pacar", None), ("recovery", "L1"), ("reactive_lp", None), ("protection", None)):
                r = c.post(f"/explorer/sessions/{sid}/trace", json={"algorithm": alg, "failed_link": fl})
                if r.status_code != 200 or not r.json()["verification"]["untraced_rerun_identical"]:
                    errors.append(f"trace {alg}: {r.status_code}")
            r = c.post("/explorer/sessions")
            if r.status_code != 200:
                errors.append("session")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    def live_worker():
        try:
            for _ in range(3):
                for path in ("/links/L4/fail", "/links/L4/restore"):
                    r = c.post(path)
                    if r.status_code != 200:
                        errors.append(f"{path} {r.status_code}")
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=explorer_worker, args=(i,)) for i in range(4)] + \
              [threading.Thread(target=live_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    assert engine.validate_allocation(live) == []
    # the frozen session still yields its original answer
    assert trace(c, sid, "pacar")["result"]["metrics"]["total_delivered"] == 130.0


# ── 7. Last live action replay ────────────────────────────────────────

def test_last_action_replay_matches_live_outcomes(client):
    c, live = client
    assert c.post("/explorer/live-action/trace").status_code == 404  # nothing recorded yet
    load_demo(c)
    actions = [
        ("post", "/simulate?algorithm=baseline", None, "simulate"),
        ("post", "/simulate?algorithm=reactive_lp", None, "simulate"),
        ("post", "/simulate?algorithm=pacar", None, "simulate"),
        ("post", "/links/L3/fail", None, "link_failure"),
        ("post", "/links/L1/fail", None, "link_failure"),       # second, uncovered failure → PACAR fallback
        ("post", "/links/L1/restore", None, "link_restore"),
        ("post", "/links/L3/restore", None, "link_restore"),
        ("post", "/failures/batch", {"link_ids": ["L4", "L5"]}, "batch_failure"),
        ("post", "/links/L4/restore", None, "link_restore"),
        ("post", "/links/L5/restore", None, "link_restore"),
        ("post", "/demands/emergency", {"id": "EM1", "source": "A", "destination": "E", "bandwidth": 5,
                                        "category": "emergency"}, "emergency_demand"),
    ]
    for _, path, body, action in actions:
        assert c.post(path, json=body).status_code == 200, path
        live_before = live_vars(live)
        r = c.post("/explorer/live-action/trace")
        assert r.status_code == 200, r.text
        t = r.json()
        json.dumps(t, allow_nan=False)
        assert t["live_action"]["action"] == action
        assert t["verification"]["replay_identical_to_live"] is True, (path, t["verification"]["differences"])
        assert t["verification"]["live_changed_since"] is False
        assert live_vars(live) == live_before  # replay never touches the live network
    # a no-op request does not replace the recorded action
    assert c.post("/links/L4/restore").json()["changed"] is False
    assert c.get("/explorer/live").json()["last_action"]["action"] == "emergency_demand"
    # once the live network moves on, the replay says so
    c.post("/nodes", json={"id": "Z"})
    t = c.post("/explorer/live-action/trace").json()
    assert t["verification"]["live_changed_since"] is True
    assert t["verification"]["replay_identical_to_live"] is True


def test_flap_hold_down_restore_replay(client):
    c, live = client
    load_demo(c)
    for _ in range(2):
        c.post("/links/L3/fail")
        c.post("/links/L3/restore")
    c.post("/links/L3/fail")
    r = c.post("/links/L3/restore").json()
    assert r["status"] == "flapping"
    t = c.post("/explorer/live-action/trace").json()
    assert t["verification"]["replay_identical_to_live"] is True
    assert any("held down" in st["title"] for st in t["steps"])


# ── 8. Existing API contracts are unchanged ───────────────────────────

def test_live_endpoint_response_keys_unchanged(client):
    c, _ = client
    load_demo(c)
    sim = c.post("/simulate?algorithm=pacar").json()
    assert set(sim) == {"version", "step", "algorithm", "algorithm_used", "route_changes", "computation_time_ms",
                        "violations", "congestion_warnings"}
    fail = c.post("/links/L3/fail").json()
    assert set(fail) == {"version", "step", "event", "changed", "link_id", "reflex_used", "reflex_time_ms",
                         "protection_scenario", "route_changes", "violations", "congestion_warnings"}
    noop = c.post("/links/L3/fail").json()
    assert noop["changed"] is False and noop["event"] == "link_already_failed"
    rest = c.post("/links/L3/restore").json()
    assert set(rest) == {"version", "step", "event", "changed", "link_id", "status", "cooldown_remaining",
                         "route_changes", "violations", "congestion_warnings"}
    assert c.post("/links/NOPE/fail").status_code == 400


def test_algorithm_catalog_is_honest():
    cat = {a["id"]: a for a in TestClient(main.app).get("/explorer/algorithms").json()}
    assert set(cat) == {"baseline", "pacar", "reactive_lp", "protection", "recovery"}
    assert cat["reactive_lp"]["trace_level"] == "stage" and cat["pacar"]["trace_level"] == "full"
    assert "run_pacar" in cat["pacar"]["source"] and "not visible" in cat["reactive_lp"]["fidelity_note"]
    assert cat["recovery"]["needs_failure"] is True


def test_pacar_score_tie_is_explained_not_hidden(client):
    c, _ = client
    for nid, x in (("A", 0), ("B", 100), ("C", 100), ("D", 200)):
        c.post("/nodes", json={"id": nid, "x": x, "y": 0 if nid != "C" else 100})
    for lid, a, b in (("L1", "A", "B"), ("L2", "B", "D"), ("L3", "A", "C"), ("L4", "C", "D")):
        c.post("/links", json={"id": lid, "source": a, "target": b, "capacity": 100, "latency": 5})
    c.post("/demands", json={"id": "D1", "source": "A", "destination": "D", "bandwidth": 10, "category": "general"})
    t = trace(c, new_session(c), "pacar")
    st = next(s for s in t["steps"] if s["kind"] == "pacar.candidates")
    scores = [row[6] for row in st["tables"][0]["rows"]]
    assert scores[0] == scores[1]  # genuinely tied
    why = next(x["text"] for x in st["sections"] if x["label"] == "Why")
    assert why.startswith("Tie:") and "strictly lower" in why
    selected = [row[1] for row in st["tables"][0]["rows"] if row[7] == "✓ selected"]
    assert selected == [st["tables"][0]["rows"][0][1]]  # the earlier candidate is kept
