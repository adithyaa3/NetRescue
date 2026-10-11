"""
Algorithm Explorer: narrative builder
=====================================
Turns the raw events recorded by ``TraceRecorder`` into presentation steps.

Every number, path and decision in a step comes from a recorded event (or, for
steps marked ``fidelity: "verification"``, from an explicit post-hoc check of a
recorded result). Nothing here re-runs or re-implements a routing algorithm.

Fidelity labels shown to the user:
  exact         a decision or value recorded directly from the real run
  stage         one stage of an LP solved by HiGHS: objective, optimum and status
                are recorded; the solver's internal iterations are not visible
  summary       a compact description of a large recorded input (e.g. tunnels)
  verification  a check computed by the explorer on a recorded result
"""

from __future__ import annotations

from typing import Any, Optional

PRIORITY_NAMES = {1: "P1 Emergency", 2: "P2 Essential", 3: "P3 Academic",
                  4: "P4 General", 5: "P5 Background"}
ALGORITHM_NAMES = {"pacar": "PACAR", "baseline": "Baseline Dijkstra",
                   "reactive_lp": "Reactive LP", "lp": "Reactive LP"}
TOL = 1e-6
MAX_SCENARIO_STEPS = 40


# ── Formatting helpers ────────────────────────────────────────────────

def mb(v: Optional[float]) -> str:
    """Mbps with up to 2 decimals; tiny non-zero values never display as 0."""
    if v is None:
        return "—"
    if v != 0 and abs(v) < 0.01:
        return f"{v:.2g} Mbps"
    r = round(v, 2)
    if abs(r - round(r)) < 1e-9:
        return f"{int(round(r))} Mbps"
    return f"{r:.2f}".rstrip("0").rstrip(".") + " Mbps"


def num(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def ms(v: Optional[float]) -> str:
    if v is None:
        return "—"
    r = round(v, 2)
    return (f"{int(round(r))}" if abs(r - round(r)) < 1e-9 else f"{r:.2f}".rstrip("0").rstrip(".")) + " ms"


def arrow(path: Optional[list]) -> str:
    return " → ".join(path) if path else "—"


def pname(p: Any) -> str:
    try:
        return PRIORITY_NAMES.get(int(p), f"P{p}")
    except (TypeError, ValueError):
        return str(p)


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def scenario_label(sc: str) -> str:
    if sc == "no_failure":
        return "no failure"
    return f"{sc[5:]} fails" if sc.startswith("fail_") else sc


# ── Builder ───────────────────────────────────────────────────────────

class NarrativeBuilder:
    def __init__(self, context: dict):
        self.ctx = context
        self.links = context["links"]            # id -> {source, target, capacity, latency}
        self.demands = context["demands"]        # id -> {source, destination, bandwidth, priority, ...}
        self.capacity = {lid: float(l["capacity"]) for lid, l in self.links.items()}
        self.loads = {lid: float(v) for lid, v in context.get("initial_loads", {}).items()}
        self.failed = set(context.get("initial_failed", []))
        self.tunnels: dict[str, dict] = {}       # "fid|tid" -> tunnel dict
        self.steps: list[dict] = []
        self._last_network: Optional[dict] = None
        self._pacar: dict = {}
        self._baseline_path: dict = {}
        self._lp: dict = {"skipped": [], "stages": {}, "floors": {}}
        self._reflex_wanted: list[dict] = []
        self._failure: dict = {}
        self._plan: dict = {}
        self._last_kind: Optional[str] = None

    # ---- generic helpers
    def dlabel(self, did: str) -> str:
        d = self.demands.get(did)
        if not d:
            return did
        return (f"{did} ({pname(d['priority'])}, {d['source']}→{d['destination']}, "
                f"{mb(d['bandwidth'])})")

    def link_ends(self, lid: str) -> str:
        l = self.links.get(lid)
        return f"{lid} ({l['source']}↔{l['target']})" if l else lid

    def tunnel(self, fid: str, tid: str) -> Optional[dict]:
        return self.tunnels.get(f"{fid}|{tid}")

    def remember_tunnels(self, tunnels_by_flow: dict) -> None:
        for fid, ts in (tunnels_by_flow or {}).items():
            for t in ts:
                self.tunnels[f"{fid}|{t['id']}"] = t

    def loads_from_allocations(self, allocations: dict) -> dict:
        loads = {lid: 0.0 for lid in self.links}
        for fid, ta in (allocations or {}).items():
            for tid, rate in ta.items():
                t = self.tunnel(fid, tid)
                if t:
                    for lid in t["link_ids"]:
                        if lid in loads:
                            loads[lid] += rate
        return loads

    def residual_to_loads(self, residual: dict) -> dict:
        return {lid: (max(0.0, self.capacity[lid] - residual.get(lid, 0.0))
                      if lid not in self.failed and lid in self.capacity else 0.0)
                for lid in self.links}

    def allocation_paths(self, allocations: dict, role: str = "allocation") -> list[dict]:
        paths = []
        for fid, ta in (allocations or {}).items():
            for tid, rate in ta.items():
                if rate is None or rate <= TOL:
                    continue
                t = self.tunnel(fid, tid)
                if t:
                    paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": role,
                                  "flow": fid, "mbps": rate, "label": f"{fid} {mb(rate)}"})
        return paths

    def add(self, ev: Optional[dict], *, group: str, phase: str, title: str, summary: str,
            sections: list, fidelity: str = "exact", table: Optional[dict] = None,
            tables: Optional[list] = None, viz: Optional[dict] = None, kind: Optional[str] = None) -> dict:
        network = {"loads": {lid: round(v, 6) for lid, v in self.loads.items()},
                   "failed": sorted(self.failed)}
        step = {
            "index": len(self.steps),
            "group": group,
            "phase": phase,
            "kind": kind or (ev["kind"] if ev else "explorer"),
            "fidelity": fidelity,
            "title": title,
            "summary": summary,
            "sections": [{"label": a, "text": b} for a, b in sections if b],
            "tables": [t for t in ([table] if table else []) + (tables or []) if t],
            "viz": viz or {},
            "event_seq": [ev["seq"]] if ev else [],
        }
        if network != self._last_network:
            step["network"] = network
            self._last_network = network
        self.steps.append(step)
        return step

    # ---- dispatch
    def build(self, events: list[dict]) -> list[dict]:
        for ev in events:
            handler = getattr(self, "on_" + ev["kind"].replace(".", "_"), None)
            if handler is not None:
                handler(ev, ev["data"])
            self._last_kind = ev["kind"]
        return self.steps

    # ════════════════════════════════════════════════════════════════
    # Controller (engine) events
    # ════════════════════════════════════════════════════════════════
    def on_engine_sim_start(self, ev, d):
        alg = ALGORITHM_NAMES.get(d["algorithm"], d["algorithm"])
        down = d.get("unavailable") or []
        self.add(ev, group="controller", phase="Controller",
                 title=f"Simulation step {d['step']}: route every demand with {alg}",
                 summary="The controller recomputes all routes from scratch on the current network.",
                 sections=[
                     ("What the controller is doing",
                      "This is the controller's real simulation step (the same code as POST "
                      f"/simulate?algorithm={d['algorithm']}; the dashboard's Start Simulation button uses PACAR): "
                      "refresh candidate tunnels, "
                      "recompute the protection plan, run the routing algorithm, install its validated result, "
                      "then make sure every verified guarantee is honored."),
                     ("Network used", ("Unavailable links (carry no traffic): " + ", ".join(down)) if down
                      else "All links are available."),
                     ("Reactivated", ("Flap hold-down ended for: " + ", ".join(d["reactivated"]))
                      if d.get("reactivated") else ""),
                 ])

    def on_engine_candidates(self, ev, d):
        self.remember_tunnels(d["tunnels"])
        rows = []
        n_total = n_inactive = 0
        for fid, ts in d["tunnels"].items():
            for t in ts:
                n_total += 1
                n_inactive += 0 if t["is_active"] else 1
                rows.append([fid, t["id"], arrow(t["node_path"]), ms(t["latency"]),
                             "yes" if t["is_active"] else "no (crosses an unavailable link)"])
        down = d.get("unavailable") or []
        self.add(ev, group="controller", phase="Algorithm A · candidate tunnels", fidelity="summary",
                 title="Candidate tunnels refreshed (Algorithm A)",
                 summary=(f"{plural(n_total, 'candidate tunnel')} for {plural(len(d['tunnels']), 'flow')}"
                          + (f"; {n_inactive} cross an unavailable link and are inactive." if n_inactive else ".")),
                 sections=[
                     ("What it is doing",
                      f"For each flow: the {d.get('k', 5)} lowest-latency simple paths of the full physical graph "
                      "(Yen's algorithm via NetworkX shortest_simple_paths; a link-disjoint alternative is tried "
                      "when only one path exists), plus the lowest-latency paths of the surviving graph when links "
                      "are down. Paths over a flow's latency limit are dropped; tunnels still referenced by the "
                      "installed allocation or the protection plan keep their IDs."),
                     ("Who uses these tunnels",
                      "The Protection Planner and the Reactive LP choose only among these tunnels. Baseline and "
                      "PACAR compute their own paths; their chosen route is mapped onto a tunnel when installed."),
                     ("Unavailable links", ", ".join(down) if down else "none"),
                 ],
                 table={"title": "Candidate tunnels", "columns": ["Flow", "Tunnel", "Path", "Latency", "Active"],
                        "rows": rows})

    def on_engine_plan_summary(self, ev, d):
        if not d["exists"]:
            reason = ("there is no P1/P2 traffic" if not d.get("has_critical")
                      else "there are no links" if not d.get("has_links") else "it was not computed")
            self.add(ev, group="controller", phase="Algorithms B + G · protection plan", fidelity="summary",
                     title="No protection plan",
                     summary=f"No plan is computed because {reason}.",
                     sections=[("Why", "The Protection Planner only protects P1 (Emergency) and P2 (Essential) "
                                       "flows. Without a verified plan, a later link failure cannot use Reflex.")])
            return
        g = d.get("guarantees") or {}
        positive = {k: v for k, v in g.items() if v > TOL}
        rows = [[fid, pname(self.demands.get(fid, {}).get("priority", "?")), mb(v)] for fid, v in g.items()]
        self.add(ev, group="controller", phase="Algorithms B + G · protection plan", fidelity="summary",
                 title=f"Protection plan recomputed: {'VERIFIED' if d.get('verified') else 'NOT verified'}",
                 summary=("Guarantees: " + ", ".join(f"{k} {mb(v)}" for k, v in positive.items())) if positive
                 else "Every planned guarantee is 0 Mbps (no flow can be protected against all single failures).",
                 sections=[
                     ("What happened",
                      f"The Protection Planner LP covered {plural(d.get('scenarios', 0), 'scenario')} (no failure + "
                      "each single usable link failing) and the Guarantee Checker re-verified it "
                      f"({'PASS' if d.get('verified') else 'FAIL'}). Select “Protection Planner” to step through it."),
                     ("Solver status", d.get("solver_status")),
                     ("Checker failures", "; ".join(d.get("failures") or [])),
                 ],
                 table={"title": "Verified guarantees", "columns": ["Flow", "Priority", "Guarantee"], "rows": rows})

    def on_engine_plan_skipped(self, ev, d):
        reason = "there is no P1/P2 traffic" if not d.get("has_critical") else "there are no links"
        self.add(ev, group="protection", phase="Protection Planner",
                 title="No protection plan",
                 summary=f"The controller skips planning because {reason}.",
                 sections=[("Why", "Only P1 (Emergency) and P2 (Essential) flows are planned; with none present "
                                   "(or no links) the plan is cleared, so Reflex cannot be used.")])

    def on_engine_lp_floors(self, ev, d):
        floors = {k: v for k, v in (d.get("floors") or {}).items() if v and v > 1e-9}
        if floors:
            text = ("A verified plan covers the current network, so each guarantee becomes a lower bound "
                    "(floor) for its flow: Σ x[f,·] ≥ guarantee.")
        elif not d.get("plan_exists"):
            text = "There is no protection plan, so the LP has no per-flow floors."
        elif not d.get("plan_verified"):
            text = "The plan is not verified, so its guarantees are not used as floors."
        elif not d.get("covered"):
            text = "The live failures are outside the plan's single-failure coverage, so no floors are added."
        else:
            text = "All guarantees are 0 Mbps, so no floor constraint is needed."
        self.add(ev, group="reactive_lp", phase="Reactive LP · setup",
                 title="Reactive LP: protection floors",
                 summary=("Floors: " + ", ".join(f"{k} ≥ {mb(v)}" for k, v in floors.items())) if floors
                 else "No floor constraints.",
                 sections=[("Why", text)])

    def on_engine_installed(self, ev, d):
        self.remember_tunnels(d.get("tunnels") or {})
        self.loads = {lid: float(v) for lid, v in (d.get("loads") or {}).items()}
        self.failed = {lid for lid, st in (d.get("statuses") or {}).items() if st != "active"}
        label = d.get("label", "")
        names = {
            "pacar_result": ("Install PACAR's result", "PACAR"),
            "baseline_result": ("Install the Baseline result", "Baseline Dijkstra"),
            "reactive_lp_result": ("Install the Reactive LP allocation", "the Reactive LP"),
            "lp_result": ("Install the Reactive LP allocation", "the Reactive LP"),
            "reflex_result": ("Install Reflex's recovery allocation", "Reflex"),
            "pacar_fallback_result": ("Install PACAR's fallback routing", "PACAR"),
            "guarantee_enforcement": ("Install the guarantee-enforcing allocation", "Reflex (enforcement)"),
        }
        title, who = names.get(label, ("Install allocation", "the algorithm"))
        rows, req, got = [], 0.0, 0.0
        for dm in sorted(d["demands"], key=lambda x: (x["priority"], x["id"])):
            req += dm["bandwidth"]
            got += dm["allocated"]
            tun = [t for t in (dm.get("tunnels") or []) if (t.get("bandwidth") or 0) > TOL]
            route = (" + ".join(f"{arrow(t['node_path'])} ({mb(t['bandwidth'])})" for t in tun) if len(tun) > 1
                     else arrow(dm.get("route")))
            rows.append([dm["id"], pname(dm["priority"]), mb(dm["bandwidth"]), mb(dm["allocated"]),
                         dm["status"].replace("_", " "), route])
        self.add(ev, group="controller", phase="Controller · installation",
                 title=title,
                 summary=f"Delivered {mb(got)} of {mb(req)} requested.",
                 sections=[
                     ("What the controller is doing",
                      f"The allocation produced by {who} is checked by the independent validator (valid simple "
                      "paths, no unavailable links, shared capacity in both directions, never more than requested) "
                      "and only then installed. Here it is installed on the explorer's private copy; the live "
                      "network is not touched."),
                     ("Effect", "Link colors now show the installed load."),
                 ],
                 table={"title": "Per-demand result", "columns": ["Demand", "Priority", "Requested", "Delivered",
                                                                  "Status", "Route"], "rows": rows},
                 viz={"paths": self.allocation_paths(d.get("allocations") or {})})

    def on_engine_enforce(self, ev, d):
        outcome = d["outcome"]
        if outcome in ("no_verified_plan", "not_covered"):
            text = ("There is no verified protection plan, so there is nothing to enforce."
                    if outcome == "no_verified_plan" else
                    "The live failures are outside the plan's coverage, so its guarantees are not in force.")
            self.add(ev, group="enforcement", phase="Guarantee enforcement",
                     title="Guarantee check: not applicable", summary=text, sections=[("Why", text)])
            return
        checked = d.get("checked") or {}
        rows = [[fid, mb(c["guarantee"]), mb(c["delivered"]),
                 "met" if c["delivered"] >= c["guarantee"] - TOL else "SHORT"] for fid, c in checked.items()]
        if outcome == "all_met":
            self.add(ev, group="enforcement", phase="Guarantee enforcement",
                     title="Guarantee check: every verified guarantee is met",
                     summary=("No change needed." if rows else "No flow has a positive guarantee."),
                     sections=[("What the controller is doing",
                                "Routing heuristics do not know individual guarantees, so after installing a "
                                "result the controller compares each planned flow's delivered rate with its "
                                f"verified guarantee (scenario: {scenario_label(d.get('scenario', ''))}).")],
                     table={"title": "Guarantee vs delivered", "columns": ["Flow", "Guarantee", "Delivered", ""],
                            "rows": rows} if rows else None)
        else:
            self.add(ev, group="enforcement", phase="Guarantee enforcement",
                     title="Guarantee check: a flow is below its guarantee",
                     summary="Short: " + ", ".join(d.get("short") or []) + " → apply the plan's reservations with Reflex.",
                     sections=[("Decision",
                                "The controller runs Reflex with the current allocation as 'previous': guaranteed "
                                "flows get their reservations first, then other traffic is kept by priority.")],
                     table={"title": "Guarantee vs delivered", "columns": ["Flow", "Guarantee", "Delivered", ""],
                            "rows": rows})

    def on_engine_enforce_result(self, ev, d):
        if not d.get("applied"):
            self.add(ev, group="enforcement", phase="Guarantee enforcement",
                     title="Enforcement could not be applied",
                     summary=f"Reflex failed: {d.get('message')}. The previous allocation stays installed.",
                     sections=[])

    def on_engine_sim_done(self, ev, d):
        v = d.get("violations") or []
        alg = ALGORITHM_NAMES.get(d.get("algorithm_used"), d.get("algorithm_used"))
        self.add(ev, group="controller", phase="Controller",
                 title="Simulation step complete",
                 summary=(f"Routed with {alg}; {plural(len(v), 'validator violation')}; "
                          f"{plural(d.get('route_changes', 0), 'route change')}."),
                 sections=[
                     ("Fallback", f"The Reactive LP produced no allocation, so PACAR was installed instead."
                      if d.get("algorithm") in ("reactive_lp", "lp") and d.get("algorithm_used") == "pacar" else ""),
                     ("Congested links (≥ 80%)", ", ".join(d.get("congestion") or []) or "none"),
                     ("Violations", "; ".join(v)),
                     ("Timing note", f"Controller computation took {ms(d.get('computation_time_ms'))} on this "
                                     "machine (simulator compute time, not network downtime)."),
                 ])

    def on_engine_noop(self, ev, d):
        reasons = {"already_failed": "the link is already failed", "already_active": "the link is already active",
                   "held_down": "the link is held down by flap damping"}
        self.add(ev, group="controller", phase="Controller",
                 title="Nothing to do",
                 summary=f"No change: {reasons.get(d.get('reason'), d.get('reason'))}.",
                 sections=[("Why", "The controller returns without advancing the step or recomputing anything.")])

    def on_engine_failure_start(self, ev, d):
        self.remember_tunnels(d.get("tunnels_before") or {})
        self.loads = {lid: float(v) for lid, v in (d.get("loads_before") or {}).items()}
        self.failed = set(d.get("unavailable_before") or [])
        self._failure = dict(d)

    def on_engine_replan_after_hold_down(self, ev, d):
        self.add(ev, group="controller", phase="Controller",
                 title="Re-plan before handling the failure",
                 summary="A flap hold-down just ended (" + ", ".join(d.get("reactivated") or []) +
                         "), so the plan is recomputed for the pre-failure network first.",
                 sections=[])

    def on_engine_link_failed(self, ev, d):
        f = self._failure
        lid = d["link_id"]
        affected = []
        for fid, ta in (f.get("allocations_before") or {}).items():
            for tid, rate in ta.items():
                t = self.tunnel(fid, tid)
                if t and lid in t["link_ids"] and rate > TOL:
                    affected.append((fid, tid, rate, t))
        self.failed = set(d.get("unavailable") or [])
        # Traffic on a tunnel that crosses the failed link is lost on every link of that tunnel.
        for fid, tid, rate, t in affected:
            for l2 in t["link_ids"]:
                if l2 in self.loads:
                    self.loads[l2] = max(0.0, self.loads[l2] - rate)
        self.loads[lid] = 0.0
        rows = [[fid, tid, arrow(t["node_path"]), mb(rate)] for fid, tid, rate, t in affected]
        self.add(ev, group="controller", phase="Failure",
                 title=f"Link {self.link_ends(lid)} fails",
                 summary=(f"It carried {mb(f.get('load_before'))}; "
                          + (f"{plural(len({a[0] for a in affected}), 'flow')} lose traffic on it."
                             if affected else "no installed traffic used it.")),
                 sections=[
                     ("What happened",
                      f"{lid} is marked FAILED on the private copy (failure count in the flap window: "
                      f"{d.get('flap_count')}). Until the controller recovers, traffic routed over it is lost; "
                      "the link colors drop that traffic from every link of the affected tunnels."),
                     ("Unavailable links now", ", ".join(d.get("unavailable") or []) or lid),
                 ],
                 table={"title": f"Traffic that used {lid}", "columns": ["Flow", "Tunnel", "Path", "Rate"],
                        "rows": rows} if rows else None,
                 viz={"links": {lid: "failing"},
                      "paths": [{"nodes": t["node_path"], "links": t["link_ids"], "role": "affected",
                                 "flow": fid, "mbps": rate, "label": f"{fid} lost {mb(rate)}"}
                                for fid, tid, rate, t in affected]})

    def on_engine_coverage(self, ev, d):
        if d["covered"]:
            decision = (f"Yes. The plan was computed for scenario “{scenario_label(d['scenario'])}” and verified, "
                        "so Reflex can recover without running any solver.")
        elif not d["plan_exists"]:
            decision = ("No protection plan exists (no P1/P2 traffic, or the network/traffic changed since the "
                        "last simulation), so Reflex cannot be used.")
        elif not d["plan_verified"]:
            decision = "The plan did not pass the Guarantee Checker, so Reflex is not allowed to use it."
        else:
            extra = sorted(set(d.get("unavailable") or []) - set(d.get("base_unavailable") or []))
            decision = (f"No. {plural(len(extra), 'link')} ({', '.join(extra)}) are down beyond the plan's "
                        "baseline; a single-failure plan covers only one additional failure.")
        self.add(ev, group="controller", phase="Coverage check",
                 title="Is this failure covered by the verified protection plan?",
                 summary=("Covered → Reflex (solver-free)" if d["covered"] else "Not covered → PACAR fallback"),
                 sections=[("Decision", decision),
                           ("Rule", "Reflex is used only if a verified plan exists and the set of unavailable links "
                                    "equals one of its covered scenarios (its baseline plus at most one link).")])

    def on_engine_pacar_fallback(self, ev, d):
        reasons = {"no_plan": "there is no protection plan", "unverified": "the plan is not verified",
                   "not_covered": "the failure set is not covered by the single-failure plan",
                   "reflex_failed": "Reflex could not produce a valid allocation"}
        self.add(ev, group="controller", phase="Fallback",
                 title="Recovery with PACAR (fallback)",
                 summary=f"Reflex is not used because {reasons.get(d.get('reason'), d.get('reason'))}.",
                 sections=[("What happens next", "PACAR re-routes every demand from scratch on the surviving "
                                                 "network; its result is installed, then the Reactive LP may improve it.")])

    def on_engine_lp_failed(self, ev, d):
        self.add(ev, group="reactive_lp", phase="Reactive LP",
                 title="Reactive LP failed",
                 summary=f"Solver status: {d.get('solver_status')}. The current allocation is kept.",
                 sections=[])

    def on_engine_lp_fallback(self, ev, d):
        self.add(ev, group="controller", phase="Fallback",
                 title="Reactive LP produced no allocation → PACAR",
                 summary=f"Solver status: {d.get('solver_status')}. The controller installs PACAR's routing instead.",
                 sections=[])

    def on_engine_failure_done(self, ev, d):
        v = d.get("violations") or []
        self.add(ev, group="controller", phase="Controller",
                 title="Failure handled",
                 summary=(("Reflex was used" if d.get("reflex_used") else "Reflex was not used")
                          + f"; {plural(len(v), 'validator violation')}; "
                            f"{plural(d.get('route_changes', 0), 'route change')}."),
                 sections=[
                     ("Reflex computation time",
                      f"{ms(d.get('reflex_time_ms'))} of computation in this simulator. This is not measured "
                      "network downtime: detection, signalling and packets are not modelled." if d.get("reflex_used")
                      else ""),
                     ("Congested links (≥ 80%)", ", ".join(d.get("congestion") or []) or "none"),
                     ("Violations", "; ".join(v)),
                 ])

    def on_engine_restore_start(self, ev, d):
        self.loads = {lid: float(v) for lid, v in (d.get("loads_before") or {}).items()}
        lid = d["link_id"]
        if d.get("held_down"):
            self.failed.add(lid)
            title, summary = (f"{self.link_ends(lid)} is held down (flap damping)",
                              f"It failed {d.get('flap_count')} times recently, so it stays out of service.")
        else:
            self.failed.discard(lid)
            title, summary = f"Link {self.link_ends(lid)} is restored", "The link is back in service."
        self.add(ev, group="controller", phase="Restore", title=title, summary=summary,
                 sections=[("What the controller does next",
                            "Refresh candidate tunnels, recompute and verify the protection plan, then re-optimize "
                            "all traffic with the Reactive LP (PACAR if the LP fails).")],
                 viz={"links": {lid: "restored" if not d.get("held_down") else "failing"}})

    def on_engine_restore_done(self, ev, d):
        v = d.get("violations") or []
        self.add(ev, group="controller", phase="Controller",
                 title="Restore handled",
                 summary=f"Link status: {d.get('status')}; {plural(len(v), 'validator violation')}; "
                         f"{plural(d.get('route_changes', 0), 'route change')}.",
                 sections=[("Violations", "; ".join(v))])

    def on_engine_batch_failed(self, ev, d):
        for lid in d.get("failed") or []:
            self.failed.add(lid)
            self.loads[lid] = 0.0
        self.add(ev, group="controller", phase="Failure",
                 title="Several links fail at once",
                 summary="Failed: " + (", ".join(d.get("failed") or []) or "none (already down)") + ".",
                 sections=[("What the controller does", "A multi-link failure is outside single-failure "
                                                        "coverage: the controller re-simulates with PACAR.")],
                 viz={"links": {lid: "failing" for lid in d.get("failed") or []}})

    def on_engine_emergency_added(self, ev, d):
        self.add(ev, group="controller", phase="Emergency demand",
                 title=f"Emergency demand {d['demand_id']} added",
                 summary=f"{d['source']}→{d['destination']}, {mb(d['bandwidth'])}, priority P1.",
                 sections=[("What the controller does", "It re-simulates with PACAR; P1 traffic is routed first, "
                                                        "so lower-priority traffic may lose capacity.")])

    def on_explorer_hypothetical_failure(self, ev, d):
        lid = d["link_id"]
        self.failed.add(lid)
        self.loads[lid] = 0.0
        self.add(ev, group="controller", phase="Scenario",
                 title=f"Scenario: {self.link_ends(lid)} is down",
                 summary=f"The explorer marks {lid} as failed on its private copy, then runs the controller's "
                         "simulation step on that network.",
                 sections=[("Why", "This compares how the routing algorithm would route all traffic on a network "
                                   f"without {lid}. It is not the controller's failure response; choose “Failure "
                                   "Recovery (Reflex)” to see that.")],
                 viz={"links": {lid: "failing"}})

    def links_for(self, path: Optional[list]) -> list:
        if not path:
            return []
        pairs = {frozenset((l["source"], l["target"])): lid for lid, l in self.links.items()}
        return [pairs[frozenset(pair)] for pair in zip(path, path[1:]) if frozenset(pair) in pairs]

    # ════════════════════════════════════════════════════════════════
    # Baseline Dijkstra
    # ════════════════════════════════════════════════════════════════
    def on_baseline_init(self, ev, d):
        self.loads = self.residual_to_loads(d["residual"])
        rows = [[i + 1, o["id"], pname(o["priority"]), f"{o['source']} → {o['destination']}",
                 mb(o["bandwidth"]), ms(o["max_latency"]) if o.get("max_latency") else "—"]
                for i, o in enumerate(d["order"])]
        self.add(ev, group="baseline", phase="Baseline Dijkstra · start",
                 title="Baseline Dijkstra starts from an empty network",
                 summary=f"{plural(len(d['order']), 'demand')} will be routed one at a time, highest priority first.",
                 sections=[
                     ("What it is doing",
                      "Each demand, in the order below, gets the single shortest path by latency (Dijkstra) "
                      "among links that still have spare capacity, and as much bandwidth as that path's tightest "
                      "link allows. Earlier demands keep what they took."),
                     ("Information used", "Link latencies (path length) and residual capacity. All capacity starts "
                                          "free: every route is recomputed from scratch."),
                     ("Constraints", "A link's capacity is shared by both directions. One path per demand; the "
                                     "baseline never splits a demand and ignores how loaded a link is (only links with no spare "
                                     "capacity at all are skipped)."),
                     ("Unavailable links", ", ".join(d.get("unusable") or []) or "none"),
                 ],
                 table={"title": "Processing order (priority, then ID)",
                        "columns": ["#", "Demand", "Priority", "Source → Destination", "Requested", "Latency limit"],
                        "rows": rows})

    def on_baseline_local(self, ev, d):
        self.add(ev, group="baseline", phase="Baseline Dijkstra",
                 title=f"{d['demand']}: local traffic",
                 summary="Source equals destination, so it is delivered without using any link.",
                 sections=[], viz=self._endpoints_viz(d["demand"]))

    def on_baseline_unreachable(self, ev, d):
        why = ("one of its endpoints has no usable link" if d["reason"] == "node_not_in_active_network"
               else "no chain of usable links connects source and destination")
        self.add(ev, group="baseline", phase="Baseline Dijkstra",
                 title=f"{d['demand']}: unreachable",
                 summary=f"Not routed: {why}.",
                 sections=[("Decision", "Marked unreachable with 0 Mbps delivered.")],
                 viz=self._endpoints_viz(d["demand"], role="unreachable"))

    def on_baseline_path(self, ev, d):
        self._baseline_path[d["demand"]] = d
        sat = d.get("saturated") or []
        if d["used_feasible_graph"]:
            how = ("Dijkstra (NetworkX shortest_path, weight = latency) ran on the links that still have spare "
                   "capacity" + (f"; saturated links {', '.join(sat)} were left out." if sat else "."))
        else:
            how = ("No path with spare capacity exists, so Dijkstra ran on all usable links; the path it returns "
                   "has no spare capacity (bottleneck 0).")
        viz = self._endpoints_viz(d["demand"])
        viz["paths"] = [{"nodes": d["path"], "links": d["links"], "role": "selected",
                         "flow": d["demand"], "label": f"{d['demand']} · {ms(d['latency'])}"}]
        viz["links"] = {lid: "saturated" for lid in sat}
        limit = d.get("max_latency")
        self.add(ev, group="baseline", phase="Baseline Dijkstra · shortest path",
                 title=f"{d['demand']}: shortest usable path",
                 summary=f"{arrow(d['path'])}, total latency {ms(d['latency'])}.",
                 sections=[
                     ("Demand", self.dlabel(d["demand"])),
                     ("What it is doing", how),
                     ("Decision", f"Path {arrow(d['path'])} ({ms(d['latency'])})."),
                     ("Why", "Dijkstra returns one minimum-latency path. The baseline does not compare alternative "
                             "paths, so congestion elsewhere does not influence this choice."),
                     ("Latency limit", (f"{ms(limit)}: " + ("satisfied." if d["latency"] <= limit else
                                                           "EXCEEDED, the demand will not be served."))
                      if limit else ""),
                 ],
                 viz=viz)

    def on_baseline_decision(self, ev, d):
        p = self._baseline_path.get(d["demand"], {})
        hops = p.get("hop_residual") or []
        bn = p.get("bottleneck")
        bn_links = [lid for lid, r in hops if bn is not None and abs(r - bn) <= 1e-9]
        before = {lid: r for lid, r in hops}
        self.loads = self.residual_to_loads(d["residual"])
        rows = [[lid, mb(before.get(lid)), mb(d["residual"].get(lid)),
                 "bottleneck" if lid in bn_links else ""] for lid, _ in hops]
        req = p.get("bandwidth", self.demands.get(d["demand"], {}).get("bandwidth"))
        outcome = d["outcome"]
        if outcome == "full":
            dec = f"Allocate the full {mb(d['allocated'])}: the bottleneck ({mb(bn)}) is at least the request."
        elif outcome == "partial":
            dec = (f"Allocate {mb(d['allocated'])} of {mb(req)}: the bottleneck "
                   f"{', '.join(bn_links)} has only {mb(bn)} left. The rest is unserved.")
        elif outcome == "no_capacity":
            dec = "Allocate nothing: the path has no spare capacity (bottleneck 0)."
        else:
            dec = "Allocate nothing: the shortest path exceeds the demand's latency limit."
        viz = self._endpoints_viz(d["demand"])
        if p.get("path") and d["allocated"] > 0:
            viz["paths"] = [{"nodes": p["path"], "links": p["links"], "role": "allocation",
                             "flow": d["demand"], "mbps": d["allocated"],
                             "label": f"{d['demand']} {mb(d['allocated'])}"}]
        elif p.get("path"):
            viz["paths"] = [{"nodes": p["path"], "links": p["links"], "role": "rejected", "flow": d["demand"],
                             "label": f"{d['demand']} 0 Mbps"}]
        viz["links"] = {lid: "bottleneck" for lid in bn_links}
        self.add(ev, group="baseline", phase="Baseline Dijkstra · allocation",
                 title=f"{d['demand']}: allocate {mb(d['allocated'])} of {mb(req)}",
                 summary=dec,
                 sections=[
                     ("What it is doing", "Bandwidth = min(requested, bottleneck), where the bottleneck is the "
                                          "smallest residual capacity on the path."),
                     ("Decision", dec),
                     ("Effect", "Residual capacity on the path drops by the allocated amount (table)."
                      if d["allocated"] > 0 else "No capacity is used."),
                 ],
                 table={"title": "Residual capacity on the path", "columns": ["Link", "Before", "After", ""],
                        "rows": rows} if rows else None,
                 viz=viz)

    def on_baseline_done(self, ev, d):
        rows = [[r["id"], mb(r["requested"]), mb(r["allocated"]), r["status"].replace("_", " "),
                 arrow(r.get("route"))] for r in d["results"]]
        tot = sum(r["allocated"] for r in d["results"])
        req = sum(r["requested"] for r in d["results"])
        self.add(ev, group="baseline", phase="Baseline Dijkstra · done",
                 title="Baseline Dijkstra finished",
                 summary=f"Delivered {mb(tot)} of {mb(req)} requested.",
                 sections=[("What happens next", "The controller maps each route onto a candidate tunnel, validates "
                                                 "the allocation and installs it.")],
                 table={"title": "Baseline result", "columns": ["Demand", "Requested", "Allocated", "Status",
                                                                "Route"], "rows": rows})

    def _endpoints_viz(self, did: str, role: str = "") -> dict:
        dm = self.demands.get(did)
        if not dm:
            return {}
        nodes = {dm["source"]: "source", dm["destination"]: "destination"}
        if role:
            nodes = {k: role for k in nodes}
        return {"nodes": nodes}

    # ════════════════════════════════════════════════════════════════
    # PACAR
    # ════════════════════════════════════════════════════════════════
    def on_pacar_init(self, ev, d):
        self._pacar = {"weights": d["weights"], "norm": d["norm"], "k": d["k_paths"]}
        self.loads = self.residual_to_loads(d["residual"])
        w, n = d["weights"], d["norm"]
        rows = [[i + 1, o["id"], pname(o["priority"]), f"{o['source']} → {o['destination']}", mb(o["bandwidth"])]
                for i, o in enumerate(d["order"])]
        groups = ", ".join(f"P{p}: {', '.join(ids)}" for p, ids in sorted(d["groups"].items()))
        self.add(ev, group="pacar", phase="PACAR · start",
                 title="PACAR starts from an empty network",
                 summary=f"Demands are handled priority group by priority group ({groups or 'none'}).",
                 sections=[
                     ("What it is doing",
                      f"For each priority group (P1 first): every demand scores up to {d['k_paths']} lowest-latency "
                      "simple paths and picks the lowest score among paths with spare capacity; then the group's "
                      "demands share the remaining capacity by progressive filling. Higher-priority groups go first "
                      "and keep what they took."),
                     ("Score (lower is better)",
                      f"{w['latency']:.2f}·latency/{num(n['max_latency'], 1)} ms (capped at 1) + "
                      f"{w['congestion']:.2f}·max link utilization + {w['hop']:.2f}·hops/{n['max_hops']} − "
                      f"{w['capacity']:.2f}·bottleneck residual/{num(n['max_capacity'], 1)} Mbps (capped at 1). "
                      f"{num(n['max_latency'], 1)} ms is the largest single-link latency and "
                      f"{num(n['max_capacity'], 1)} Mbps the largest link capacity in this network."),
                     ("Information used", "Latency, hop count and residual capacity of each candidate path. All "
                                          "capacity starts free: every route is recomputed from scratch."),
                     ("Constraints", "Shared capacity in both directions; one path per demand (PACAR never "
                                     "splits a demand across paths)."),
                     ("Unavailable links", ", ".join(d.get("unusable") or []) or "none"),
                 ],
                 table={"title": "Processing order", "columns": ["#", "Demand", "Priority", "Source → Destination",
                                                                 "Requested"], "rows": rows})

    def on_pacar_group_start(self, ev, d):
        self.loads = self.residual_to_loads(d["residual"])
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} group",
                 title=f"Priority group {pname(d['priority'])}",
                 summary=f"{plural(len(d['demands']), 'demand')}: {', '.join(d['demands'])}.",
                 sections=[("What it is doing", "First each demand in the group picks a path (scoring); then the "
                                                "group shares the capacity left by higher-priority groups.")])

    def on_pacar_local(self, ev, d):
        self.add(ev, group="pacar", phase="PACAR", title=f"{d['demand']}: local traffic",
                 summary="Source equals destination, so it is delivered without using any link.",
                 sections=[], viz=self._endpoints_viz(d["demand"]))

    def on_pacar_unreachable(self, ev, d):
        why = ("one of its endpoints has no usable link" if d["reason"] == "node_not_in_active_network"
               else "no chain of usable links connects source and destination (partitioned network)")
        self.add(ev, group="pacar", phase="PACAR", title=f"{d['demand']}: unreachable",
                 summary=f"Not routed: {why}.", sections=[("Decision", "0 Mbps delivered.")],
                 viz=self._endpoints_viz(d["demand"], role="unreachable"))

    def _term_text(self, chosen: dict, other: dict) -> str:
        w = self._pacar.get("weights", {})
        tc, to = chosen.get("terms") or {}, other.get("terms") or {}
        if "norm_latency" not in tc or "norm_latency" not in to:
            return ""
        parts = []
        dl = w["latency"] * (tc["norm_latency"] - to["norm_latency"])
        du = w["congestion"] * (tc["max_util"] - to["max_util"])
        dh = w["hop"] * (tc["norm_hops"] - to["norm_hops"])
        dc = -w["capacity"] * (tc["norm_capacity"] - to["norm_capacity"])
        for name, val in (("latency", dl), ("congestion", du), ("hops", dh), ("spare capacity", dc)):
            if abs(val) > 5e-4:
                parts.append(f"{name} {val:+.3f}")
        text = (f"Compared with {arrow(other['path'])} (score {num(other['score'])}), the selected path's score "
                f"differs by: {', '.join(parts) if parts else 'nothing measurable'}.")
        if tc["norm_latency"] >= 1 and to["norm_latency"] >= 1:
            text += (f" Both latencies are at or above {num(self._pacar['norm']['max_latency'], 1)} ms, so both get "
                     f"the full latency term ({w['latency']:.2f}); latency does not separate them.")
        return text

    def on_pacar_candidates(self, ev, d):
        cands = d["candidates"]
        best = d.get("best_path")
        w = self._pacar.get("weights", {})
        rows, paths = [], []
        chosen = None
        for i, c in enumerate(cands):
            t = c.get("terms") or {}
            is_best = best is not None and c["path"] == best
            if is_best:
                chosen = c
            rows.append([i + 1, arrow(c["path"]), ms(t.get("latency")), t.get("hops", len(c["path"]) - 1),
                         mb(c["bottleneck"]), f"{100 * t['max_util']:.0f}%" if "max_util" in t else "—",
                         num(c["score"]), "✓ selected" if is_best else ("no spare capacity" if c["bottleneck"] <= 0 else "")])
            paths.append({"nodes": c["path"], "links": c["links"], "role": "selected" if is_best else
                          ("rejected" if c["bottleneck"] <= 0 else "candidate"),
                          "flow": d["demand"], "label": f"#{i + 1} · {num(c['score'])}"})
        for c in d.get("dropped_by_latency") or []:
            rows.append(["—", arrow(c["path"]), ms(c["latency"]), len(c["path"]) - 1, "—", "—", "—",
                         "over latency limit"])
        if chosen is not None:
            decision = (f"Selected {arrow(best)} (score {num(d['best_score'])}): the lowest score among the "
                        f"{plural(sum(1 for c in cands if c['bottleneck'] > 0), 'candidate')} with spare capacity.")
            why = ""
            if cands and cands[0]["path"] != best:
                why = self._term_text(chosen, cands[0])
            elif len(cands) > 1:
                runner = min((c for c in cands if c["path"] != best and c.get("score") is not None),
                             key=lambda c: c["score"], default=None)
                if runner is not None:
                    why = "The shortest path also has the lowest score. " + self._term_text(chosen, runner)
            else:
                why = "It is the only candidate path."
            ties = [c for c in cands if c["path"] != best and c["bottleneck"] > 0 and c.get("score") is not None
                    and d.get("best_score") is not None and abs(c["score"] - d["best_score"]) <= 1e-12]
            if ties:
                why = (f"Tie: {', '.join(arrow(c['path']) for c in ties)} "
                       f"{'has' if len(ties) == 1 else 'have'} exactly the same score ({num(d['best_score'])}). PACAR "
                       "replaces its current best only when a later candidate scores strictly lower, so the earlier "
                       "candidate in latency order (Yen's order) is kept. " + why)
            fits = [c for c in cands if c["path"] != best and c["bottleneck"] >= d["bandwidth"] - 1e-9]
            if d.get("best_bottleneck") is not None and d["best_bottleneck"] < d["bandwidth"] - 1e-9:
                note = (f" Note: the selected path has only {mb(d['best_bottleneck'])} free, less than the "
                        f"{mb(d['bandwidth'])} requested")
                if fits:
                    note += (f", while {arrow(fits[0]['path'])} has {mb(fits[0]['bottleneck'])} free; the PACAR score "
                             "does not require a path to fit the whole demand, so the lower score wins")
                why += note + "."
        else:
            decision = "No candidate path has spare capacity on every link, so PACAR runs its fallback search next."
            why = "A path is only eligible if every link on it still has residual capacity above zero."
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} path scoring",
                 title=f"{d['demand']}: score {plural(len(cands), 'candidate path')}",
                 summary=decision,
                 sections=[
                     ("Demand", self.dlabel(d["demand"])),
                     ("What it is doing", f"Up to {self._pacar.get('k', 5)} lowest-latency simple paths (Yen's "
                                          "algorithm) are scored with the PACAR formula; lower is better."),
                     ("Information used", "Residual capacity after higher-priority groups (same-group demands "
                                          "have not been allocated yet), path latency and hop count."),
                     ("Decision", decision),
                     ("Why", why),
                     ("Constraints", (f"Latency limit {ms(d['max_latency'])} filters candidates. " if d.get("max_latency") else "")
                      + "No bandwidth is allocated yet; the group shares capacity in the filling phase."),
                 ],
                 table={"title": f"Candidates (score = {w.get('latency', 0):.2f}·lat + {w.get('congestion', 0):.2f}·util + "
                                 f"{w.get('hop', 0):.2f}·hops − {w.get('capacity', 0):.2f}·cap)",
                        "columns": ["#", "Path", "Latency", "Hops", "Bottleneck", "Max util", "Score", ""],
                        "rows": rows},
                 viz={**self._endpoints_viz(d["demand"]), "paths": paths})

    def on_pacar_fallback_search(self, ev, d):
        rows = [[i + 1, arrow(x["path"]), ms(x.get("latency")), mb(x.get("bottleneck")),
                 {"accepted": "✓ enough capacity", "insufficient_capacity": "not enough capacity",
                  "exceeds_latency_limit": "over latency limit"}.get(x["verdict"], x["verdict"])]
                for i, x in enumerate(d.get("examined") or [])]
        found = d.get("found")
        self.add(ev, group="pacar", phase="PACAR · fallback search",
                 title="Fallback: search further paths for enough capacity",
                 summary=(f"Found {arrow(found)} with at least {mb(d['required'])} free." if found else
                          f"No path among the first {d['max_paths']} shortest has {mb(d['required'])} free."),
                 sections=[("What it is doing",
                            f"Walks the shortest simple paths in latency order (up to {d['max_paths']}) and takes "
                            f"the first whose bottleneck is at least the full request ({mb(d['required'])})."),
                           ("Limit", "The 20-path limit was reached." if d.get("limit_reached") else "")],
                 table={"title": "Paths examined", "columns": ["#", "Path", "Latency", "Bottleneck", "Verdict"],
                        "rows": rows} if rows else None,
                 viz={"paths": ([{"nodes": found, "links": self.links_for(found), "role": "selected",
                                  "label": "fallback"}] if found else [])})

    def on_pacar_fallback(self, ev, d):
        texts = {
            "capacity_feasible_path": f"Use the fallback path {arrow(d.get('path'))}.",
            "partial_candidate": f"Use the best-scoring candidate with some spare capacity: {arrow(d.get('path'))}.",
            "zero_capacity_candidate": (f"Keep the shortest candidate {arrow(d.get('path'))} even though it has no "
                                        "spare capacity; it will receive 0 Mbps unless capacity frees up."),
            "no_candidates": "No candidate path exists within the latency limit; the demand cannot be served.",
        }
        viz = {}
        if d.get("path"):
            viz["paths"] = [{"nodes": d["path"], "links": d.get("links") or [],
                             "role": "selected" if d.get("bottleneck", 0) > 0 else "rejected",
                             "flow": d["demand"], "label": d["demand"]}]
        self.add(ev, group="pacar", phase="PACAR · fallback",
                 title=f"{d['demand']}: fallback decision",
                 summary=texts.get(d.get("outcome"), str(d.get("outcome"))), sections=[], viz=viz)

    def on_pacar_unserved(self, ev, d):
        self.add(ev, group="pacar", phase="PACAR", title=f"{d['demand']}: no path",
                 summary="No feasible path was found; the demand is unserved (congestion).", sections=[],
                 viz=self._endpoints_viz(d["demand"], role="unreachable"))

    def on_pacar_group_empty(self, ev, d):
        pass  # nothing routable in this group; previous steps already explained why

    def on_pacar_fill_start(self, ev, d):
        self._pacar["fill_paths"] = d["paths"]
        self._pacar["requested"] = d["requested"]
        rows = [[did, arrow(p["path"]), mb(d["requested"][did]), num(p.get("score"))] for did, p in d["paths"].items()]
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} progressive filling",
                 title=f"{pname(d['priority'])}: share capacity by progressive filling",
                 summary=(f"{len(d['paths'])} demand grows on its selected path." if len(d["paths"]) == 1 else
                          f"{len(d['paths'])} demands grow together on their selected paths."),
                 sections=[("What it is doing",
                            "Every eligible demand receives the same increment each round. A round stops at the "
                            "first link that becomes full (its free capacity divided by the number of demands using "
                            "it) or the first demand that is satisfied. Demands whose path has a full link stop "
                            "growing."),
                           ("Why", "This approximates max-min fairness among equal-priority demands at their "
                                   "bottlenecks; it is not a global optimum.")],
                 table={"title": "Selected paths", "columns": ["Demand", "Path", "Requested", "Score"], "rows": rows},
                 viz={"paths": [{"nodes": p["path"], "links": p["links"], "role": "selected", "flow": did,
                                 "label": did} for did, p in d["paths"].items()]})

    def on_pacar_fill_round(self, ev, d):
        self.loads = self.residual_to_loads(d["residual"])
        delta = d["delta"]
        lim_links = [lid for lid, (_, _, share) in d["shares"].items() if abs(share - delta) <= 1e-9]
        lim_demands = [did for did, need in d["needs"].items() if abs(need - delta) <= 1e-9]
        why = []
        if lim_links:
            why.append("link " + ", ".join(f"{lid} (free {mb(d['shares'][lid][0])} ÷ {d['shares'][lid][1]})"
                                           for lid in lim_links) + " becomes full")
        if lim_demands:
            why.append(", ".join(lim_demands) + " is satisfied")
        share_rows = [[lid, mb(v[0]), v[1], mb(v[2]), "limits this round" if lid in lim_links else ""]
                      for lid, v in sorted(d["shares"].items(), key=lambda x: x[1][2])]
        alloc_rows = [[did, mb(d["needs"].get(did)), mb(d["allocation"][did]),
                       mb(self._pacar.get("requested", {}).get(did))] for did in d["eligible"]]
        paths = []
        for did, p in (self._pacar.get("fill_paths") or {}).items():
            got = d["allocation"].get(did, 0.0)
            paths.append({"nodes": p["path"], "links": p["links"], "role": "allocation" if got > TOL else "selected",
                          "flow": did, "mbps": got, "label": f"{did} {mb(got)}"})
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} progressive filling",
                 title=f"Round {d['round']}: +{mb(delta)} to {plural(len(d['eligible']), 'demand')}",
                 summary=f"Increment {mb(delta)}: the round stops when " + " and ".join(why) + ".",
                 sections=[("Eligible", ", ".join(d["eligible"])),
                           ("Decision", f"increment = min(fair share of every link, remaining need of every demand) "
                                        f"= {mb(delta)}."),
                           ("Effect", "Each eligible demand grows by the increment; residual capacity on its path "
                                      "drops accordingly.")],
                 tables=[{"title": "Fair share per link", "columns": ["Link", "Free", "Demands", "Share", ""],
                          "rows": share_rows},
                         {"title": "Demands", "columns": ["Demand", "Needed (before)", "Allocated (after)",
                                                          "Requested"], "rows": alloc_rows}],
                 viz={"paths": paths, "links": {lid: "bottleneck" for lid in lim_links}})

    def on_pacar_fill_stop(self, ev, d):
        unmet = {did: n for did, n in (d.get("needs") or {}).items() if n > 1e-9}
        if d["reason"] == "no_eligible_demand":
            text = ("Filling stops: every demand is satisfied." if not unmet else
                    "Filling stops: each unsatisfied demand has a full link on its path ("
                    + ", ".join(d.get("saturated") or []) + "). PACAR does not move it to another path.")
        else:
            text = "Filling stops: no further increment is possible."
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} progressive filling",
                 title="Filling complete", summary=text,
                 sections=[("Still unmet", ", ".join(f"{k} needs {mb(v)} more" for k, v in unmet.items()))],
                 viz={"links": {lid: "saturated" for lid in d.get("saturated") or []}})

    def on_pacar_group_result(self, ev, d):
        rows = [[r["id"], arrow(r.get("path")), mb(r["allocated"]), mb(r["requested"]),
                 r["status"].replace("_", " ")] for r in d["results"]]
        self.add(ev, group="pacar", phase=f"PACAR · P{d['priority']} result",
                 title=f"{pname(d['priority'])}: result",
                 summary=f"{sum(1 for r in d['results'] if r['status'] == 'fully_served')} of "
                         f"{len(d['results'])} fully served.",
                 sections=[("Effect", "The capacity this group used is no longer available to lower priorities.")],
                 table={"title": "Group result", "columns": ["Demand", "Path", "Allocated", "Requested", "Status"],
                        "rows": rows},
                 viz={"paths": [{"nodes": r["path"], "links": self.links_for(r["path"]), "role": "allocation", "flow": r["id"],
                                 "mbps": r["allocated"], "label": f"{r['id']} {mb(r['allocated'])}"}
                                for r in d["results"] if r.get("path") and r["allocated"] > TOL]})

    def on_pacar_done(self, ev, d):
        tot = sum(r["allocated"] for r in d["results"])
        req = sum(r["requested"] for r in d["results"])
        rows = [[r["id"], pname(r["priority"]), mb(r["requested"]), mb(r["allocated"]),
                 r["status"].replace("_", " "), arrow(r.get("route"))]
                for r in sorted(d["results"], key=lambda r: (r["priority"], r["id"]))]
        self.add(ev, group="pacar", phase="PACAR · done", title="PACAR finished",
                 summary=f"Delivered {mb(tot)} of {mb(req)} requested.",
                 sections=[("What happens next", "The controller maps each route onto a candidate tunnel, "
                                                 "validates the allocation and installs it.")],
                 table={"title": "PACAR result", "columns": ["Demand", "Priority", "Requested", "Allocated",
                                                             "Status", "Route"], "rows": rows})

    # ════════════════════════════════════════════════════════════════
    # Reactive LP
    # ════════════════════════════════════════════════════════════════
    def on_lp_inputs(self, ev, d):
        self._lp = {"skipped": [], "stages": {}, "floors": d.get("floors") or {}, "flows": d["flows"],
                    "failed": d.get("failed_links") or []}
        rows, excl_rows, paths = [], [], []
        for f in d["flows"]:
            for t in f["variables"]:
                self.tunnels[f"{f['id']}|{t['id']}"] = t
                rows.append([f"x[{f['id']},{t['id']}]", f["id"], pname(f["priority"]), arrow(t["node_path"]),
                             ms(t["latency"]), f"0 … {mb(f['bandwidth'])}"])
                paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": "candidate",
                              "flow": f["id"], "label": t["id"]})
            for e in f["excluded"]:
                excl_rows.append([f["id"], e["tunnel"]["id"], arrow(e["tunnel"]["node_path"]),
                                  "; ".join(e["reasons"]) or "excluded"])
        self.add(ev, group="reactive_lp", phase="Reactive LP · model",
                 title=f"Reactive LP: {plural(d['num_vars'], 'variable')}, one per usable tunnel",
                 summary="x[f,t] = Mbps that flow f sends on candidate tunnel t. A flow may split over several tunnels.",
                 sections=[("What it is doing",
                            "Builds a multicommodity-flow linear program over the candidate tunnels that avoid "
                            "every unavailable link."),
                           ("Unavailable links", ", ".join(d.get("failed_links") or []) or "none")],
                 tables=[{"title": "Variables", "columns": ["Variable", "Flow", "Priority", "Tunnel path",
                                                            "Latency", "Bounds"], "rows": rows},
                         {"title": "Tunnels left out", "columns": ["Flow", "Tunnel", "Path", "Reason"],
                          "rows": excl_rows} if excl_rows else None],
                 viz={"paths": paths})

    def on_lp_no_variables(self, ev, d):
        self.add(ev, group="reactive_lp", phase="Reactive LP",
                 title="Reactive LP: no usable tunnel",
                 summary=("No flow has a usable candidate tunnel; the LP returns an empty allocation" +
                          ("" if d.get("success") else " and reports failure because a positive floor cannot be met") + "."),
                 sections=[])

    def on_lp_model(self, ev, d):
        rows = [[r["link"], mb(r["capacity"]), len(r["variables"]), ", ".join(r["variables"][:6]) +
                 (" …" if len(r["variables"]) > 6 else "")] for r in d["capacity_rows"]]
        floor_rows = [[r["flow"], mb(r["floor"])] for r in d["floor_rows"]]
        self.add(ev, group="reactive_lp", phase="Reactive LP · model", fidelity="summary",
                 title="Constraints",
                 summary=(f"{plural(len(d['demand_rows']), 'demand row')}, "
                          f"{plural(len(d['capacity_rows']), 'shared-capacity row')}, "
                          f"{plural(len(d['floor_rows']), 'floor row')}."),
                 sections=[("Demand", "Σ_t x[f,t] ≤ requested[f] for every flow."),
                           ("Shared capacity", "For every usable link e: Σ x[f,t] over all tunnels through e ≤ "
                                               "capacity[e]. Both directions share the same capacity."),
                           ("Floors", "Σ_t x[f,t] ≥ guarantee[f] for flows with a verified guarantee in force."
                            if d["floor_rows"] else "none"),
                           ("Bounds", "0 ≤ x[f,t] ≤ requested[f].")],
                 tables=[{"title": "Shared-capacity rows", "columns": ["Link", "Capacity", "#Variables", "Variables"],
                          "rows": rows},
                         {"title": "Floor rows", "columns": ["Flow", "Floor"], "rows": floor_rows} if floor_rows else None])

    def on_lp_stage(self, ev, d):
        p = d["priority"]
        if d.get("skipped"):
            self._lp["skipped"].append(p)
            return
        self._lp["stages"][p] = d
        flows = d["flows"]
        sol = d.get("solver") or {}
        paths = []
        for f in self._lp.get("flows", []):
            if f["id"] in flows:
                for t in f["variables"]:
                    paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": "candidate",
                                  "flow": f["id"], "label": t["id"]})
        earlier = sorted(k for k in self._lp["stages"] if k < p)
        if d.get("optimum") is None:
            self.add(ev, group="reactive_lp", phase=f"Reactive LP · stage P{p}", fidelity="stage",
                     title=f"Stage P{p} failed",
                     summary=f"HiGHS could not solve this stage: {sol.get('message')}. The LP reports failure.",
                     sections=[])
            return
        self.add(ev, group="reactive_lp", phase=f"Reactive LP · stage P{p}", fidelity="stage",
                 title=f"Stage P{p}: maximize total {pname(p)} delivery",
                 summary=f"Optimum {mb(d['optimum'])} of {mb(d.get('requested'))} requested; locked for later stages.",
                 sections=[
                     ("What it is doing",
                      f"HiGHS solves: maximize Σ x[f,t] over the {pname(p)} flows ({', '.join(flows)}) subject to all "
                      "constraints" + (f" and the locked optima of {', '.join('P' + str(k) for k in earlier)}" if earlier
                                       else "") + "."),
                     ("Decision", f"The best achievable {pname(p)} total is {mb(d['optimum'])}. A new constraint "
                                  f"Σ x(P{p}) ≥ {mb(d['optimum'])} is added, so lower priorities can never reduce it."),
                     ("Solver", f"status {sol.get('status')} ({sol.get('message')}); "
                                f"{sol.get('iterations')} solver iterations reported."),
                     ("What is not shown", "Only the stage's optimal value is recorded. The solver's internal "
                                           "iterations, and which of several equally good allocations it found at "
                                           "this stage, are not shown because they are not unique."),
                 ],
                 viz={"paths": paths})

    def on_lp_latency_stage(self, ev, d):
        sol = d.get("solver") or {}
        skipped = self._lp.get("skipped") or []
        self.add(ev, group="reactive_lp", phase="Reactive LP · latency stage", fidelity="stage",
                 title="Final stage: minimize latency without losing any class optimum",
                 summary=(f"Objective Σ 0.01·latency·x = {num(d.get('objective'))}." if d.get("objective") is not None
                          else "This stage did not solve; the last stage's solution is kept (priorities only)."),
                 sections=[("What it is doing",
                            "Among all allocations that keep every locked priority total, HiGHS picks one with the "
                            "lowest latency-weighted traffic (0.01 × tunnel latency per Mbps)."),
                           ("Solver", f"status {sol.get('status')} ({sol.get('message')}); "
                                      f"{sol.get('iterations')} iterations reported; result: {d.get('solver_status')}."),
                           ("Skipped stages", ("No flows in " + ", ".join(f"P{p}" for p in skipped) + ".") if skipped else "")])

    def on_lp_result(self, ev, d):
        for key, t in (d.get("tunnels") or {}).items():
            self.tunnels[key] = t
        alloc = d["allocations"]
        self.loads = self.loads_from_allocations(alloc)
        rows = []
        for fid, ta in alloc.items():
            for tid, rate in sorted(ta.items()):
                t = self.tunnel(fid, tid)
                rows.append([fid, tid, arrow(t["node_path"]) if t else "?", mb(rate)])
        pri = d.get("delivered_by_priority") or {}
        self.add(ev, group="reactive_lp", phase="Reactive LP · result",
                 title="LP allocation",
                 summary=f"Delivered {mb(d['total_delivered'])}; unserved {mb(d['total_unserved'])} ({d['solver_status']}).",
                 sections=[("Per priority", ", ".join(f"{k} {mb(v)}" for k, v in sorted(pri.items()) if v > TOL) or "nothing delivered"),
                           ("Effect", "Link colors show the LP's load. The controller still has to validate and "
                                      "install it (or, after a failure, the install policy decides).")],
                 table={"title": "Tunnel rates", "columns": ["Flow", "Tunnel", "Path", "Rate"], "rows": rows},
                 viz={"paths": self.allocation_paths(alloc)})
        self._lp_verification(d)

    def _lp_verification(self, d):
        alloc = d["allocations"]
        loads = self.loads_from_allocations(alloc)
        rows, binding, over = [], [], []
        for lid in self.links:
            if lid in self._lp.get("failed", []):
                continue
            cap = self.capacity[lid]
            load = loads.get(lid, 0.0)
            tag = ""
            if load > cap + 1e-5:
                over.append(lid)
                tag = "OVER CAPACITY"
            elif load >= cap - 1e-6 and load > 0:
                binding.append(lid)
                tag = "binding (full)"
            rows.append([lid, mb(load), mb(cap), mb(cap - load), tag])
        checks = []
        flows = {f["id"]: f for f in self._lp.get("flows", [])}
        over_demand = [fid for fid, ta in alloc.items() if fid in flows and sum(ta.values()) > flows[fid]["bandwidth"] + 1e-5]
        floors = {k: v for k, v in (self._lp.get("floors") or {}).items() if v and v > 0}
        floor_miss = [fid for fid, fl in floors.items() if sum((alloc.get(fid) or {}).values()) < fl - 1e-5]
        stage_rows = []
        for p, st in sorted(self._lp["stages"].items()):
            got = sum(sum((alloc.get(fid) or {}).values()) for fid in st["flows"])
            ok = st.get("optimum") is not None and got >= st["optimum"] - 1e-5
            stage_rows.append([f"P{p}", mb(st.get("optimum")), mb(got), "✓" if ok else "✗"])
        checks.append(("Capacity", "every link load ≤ capacity ✓" if not over else f"exceeded on {', '.join(over)} ✗"))
        checks.append(("Demand", "no flow receives more than requested ✓" if not over_demand
                       else f"exceeded for {', '.join(over_demand)} ✗"))
        if floors:
            checks.append(("Floors", "every floor is met ✓" if not floor_miss else f"missed for {', '.join(floor_miss)} ✗"))
        checks.append(("Binding links", ", ".join(binding) if binding else "none (no link is full)"))
        self.add(None, group="verification", phase="Explorer check", fidelity="verification",
                 kind="explorer.lp_check",
                 title="Check the returned solution (explorer, post-hoc)",
                 summary=("All constraints hold on the returned allocation." if not (over or over_demand or floor_miss)
                          else "A constraint check failed; see details."),
                 sections=[("What this is", "Computed by the explorer from the LP's returned allocation and the "
                                            "link capacities. It is not a step of the LP itself.")] + checks,
                 tables=[{"title": "Link loads", "columns": ["Link", "Load", "Capacity", "Slack", ""], "rows": rows},
                         {"title": "Each class keeps its stage optimum", "columns": ["Class", "Stage optimum",
                                                                                     "Final", ""],
                          "rows": stage_rows} if stage_rows else None],
                 viz={"links": {**{lid: "binding" for lid in binding}, **{lid: "over" for lid in over}},
                      "paths": self.allocation_paths(alloc)})

    # ════════════════════════════════════════════════════════════════
    # Protection Planner + Guarantee Checker
    # ════════════════════════════════════════════════════════════════
    def on_plan_inputs(self, ev, d):
        self._plan = {"critical": {c["id"]: c for c in d["critical"]}}
        rows = [[c["id"], pname(c["priority"]), f"{c['source']} → {c['destination']}", mb(c["bandwidth"]), "planned"]
                for c in d["critical"]]
        rows += [[n["id"], pname(n["priority"]), "", "", "not planned (P3–P5)"] for n in d["not_planned"]]
        self.add(ev, group="protection", phase="Protection Planner · inputs",
                 title="Which flows receive guarantees",
                 summary=(f"{plural(len(d['critical']), 'P1/P2 flow')} will be planned; "
                          f"{len(d['not_planned'])} lower-priority flows are not."),
                 sections=[("What it is doing",
                            "The Protection Planner computes, before any failure, how much bandwidth each P1 "
                            "(Emergency) and P2 (Essential) flow can keep under every single-link failure, and "
                            "which tunnels must reserve that bandwidth.")],
                 table={"title": "Flows", "columns": ["Flow", "Priority", "Endpoints", "Requested", "Plan"], "rows": rows})

    def on_plan_trivial(self, ev, d):
        self.add(ev, group="protection", phase="Protection Planner", title="Nothing to plan",
                 summary="There are no P1/P2 flows; the plan is empty and is not verified.", sections=[])

    def on_plan_scenarios(self, ev, d):
        self._plan["scenarios"] = d["scenarios"]
        self.add(ev, group="protection", phase="Protection Planner · scenarios",
                 title=f"{plural(len(d['scenarios']), 'scenario')} to survive",
                 summary="No failure, plus each usable link failing on its own.",
                 sections=[("Scenarios", ", ".join(scenario_label(s) for s in d["scenarios"])),
                           ("Already unavailable", (", ".join(d["unavailable"]) + ": the plan's baseline; reservations "
                                                    "never use them.") if d["unavailable"] else "none"),
                           ("Not covered", "Two or more simultaneous new failures are outside this plan.")])

    def on_plan_variables(self, ev, d):
        rows, excl, paths = [], [], []
        for fid, v in d["flows"].items():
            for t in v["tunnels"]:
                self.tunnels[f"{fid}|{t['id']}"] = t
                rows.append([f"a[{fid},{t['id']}]", fid, arrow(t["node_path"]), ms(t["latency"])])
                paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": "candidate", "flow": fid,
                              "label": t["id"]})
            rows.append([f"b[{fid}]", fid, "guaranteed Mbps", f"0 … {mb(self._plan.get('critical', {}).get(fid, {}).get('bandwidth'))}"])
            for e in v["excluded"]:
                excl.append([fid, e["tunnel"]["id"], arrow(e["tunnel"]["node_path"]), "; ".join(e["reasons"])])
        self.add(ev, group="protection", phase="Protection Planner · model",
                 title=f"Variables: reservations a[f,t] and guarantees b[f] ({d['num_vars']} in total)",
                 summary="a[f,t] = Mbps reserved for flow f on tunnel t; b[f] = Mbps guaranteed to flow f.",
                 sections=[("What it is doing", "In the planning LP every reservation counts against link "
                                                "capacity (Constraint 1); the guarantee is what the flow keeps in its "
                                                "worst scenario (Constraint 2).")],
                 tables=[{"title": "Variables", "columns": ["Variable", "Flow", "Tunnel path / meaning", "Latency / bounds"],
                          "rows": rows},
                         {"title": "Tunnels left out", "columns": ["Flow", "Tunnel", "Path", "Reason"], "rows": excl}
                         if excl else None],
                 viz={"paths": paths})

    def on_plan_no_tunnels(self, ev, d):
        self.add(ev, group="protection", phase="Protection Planner", title="No usable tunnel",
                 summary="No planned flow has a usable tunnel; every guarantee is 0 and the plan is not verified.",
                 sections=[])

    def on_plan_capacity_rows(self, ev, d):
        rows = [[r["link"], mb(r["capacity"]), len(r["variables"]),
                 ", ".join(f"a[{f},{t}]" for f, t in r["variables"][:5]) + (" …" if len(r["variables"]) > 5 else "")]
                for r in d["rows"]]
        self._plan["capacity_rows"] = rows
        # shown together with the survival constraints

    def on_plan_survival(self, ev, d):
        matrix, scenarios = d["matrix"] or {}, d["scenarios"]
        self._plan["survival"] = matrix
        all_tunnels = {fid: sorted({tid for sc in by_sc.values() for tid in sc}) for fid, by_sc in matrix.items()}
        affected, unaffected = [], []
        for sc in scenarios:
            lost = {fid: [tid for tid in all_tunnels[fid] if tid not in matrix[fid].get(sc, [])] for fid in matrix}
            if sc != "no_failure" and not any(lost.values()):
                unaffected.append(sc)
            else:
                affected.append((sc, lost))
        if self._plan.get("capacity_rows") is not None:
            self.add(ev, group="protection", phase="Protection Planner · constraints", fidelity="summary",
                     kind="plan.capacity_constraint",
                     title="Constraint 1: reservations share link capacity",
                     summary="For every usable link: Σ a[f,t] over tunnels through it ≤ capacity (both directions).",
                     sections=[("Why", "The planner requires every reservation to fit at the same time, so "
                                       "reservations of different flows (and different tunnels) on the same link add up."),
                               ("Note", "Day-to-day routing (PACAR, Baseline, Reactive LP) does not subtract "
                                        "reservations from link capacity; Reflex uses them after a failure.")],
                     table={"title": "Capacity rows", "columns": ["Link", "Capacity", "#Reservations", "Variables"],
                            "rows": self._plan["capacity_rows"]})
        shown = affected[:MAX_SCENARIO_STEPS]
        for sc, lost in shown:
            failed = sc[5:] if sc.startswith("fail_") else None
            rows, paths, parts = [], [], []
            for fid in matrix:
                surv = matrix[fid].get(sc, [])
                parts.append(f"{fid}: " + (", ".join(surv) if surv else "no surviving tunnel → b ≤ 0"))
                for tid in all_tunnels[fid]:
                    t = self.tunnel(fid, tid)
                    ok = tid in surv
                    rows.append([fid, tid, arrow(t["node_path"]) if t else tid, "survives" if ok else f"lost ({failed} down)"])
                    if t:
                        paths.append({"nodes": t["node_path"], "links": t["link_ids"],
                                      "role": "backup" if ok else "rejected", "flow": fid, "label": tid})
            self.add(ev, group="protection", phase="Protection Planner · survival constraints", fidelity="exact",
                     kind="plan.survival_scenario",
                     title=f"Constraint 2 · scenario “{scenario_label(sc)}”",
                     summary="; ".join(parts) + ".",
                     sections=[("Rule", "For every planned flow f: Σ a[f,t] over the tunnels that survive this "
                                        "scenario ≥ b[f]. So the guarantee must fit in what remains reserved.")],
                     table={"title": "Tunnels in this scenario", "columns": ["Flow", "Tunnel", "Path", ""], "rows": rows},
                     viz={"paths": paths, "links": {failed: "failing"} if failed else {}})
        rest = [sc for sc, _ in affected[MAX_SCENARIO_STEPS:]] + unaffected
        if rest:
            self.add(ev, group="protection", phase="Protection Planner · survival constraints", kind="plan.survival_rest",
                     title=f"Constraint 2 · {plural(len(rest), 'other scenario')}",
                     summary=("No planned tunnel crosses these links, so every reservation survives: "
                              if not affected[MAX_SCENARIO_STEPS:] else "Also constrained (collapsed for length): ")
                             + ", ".join(scenario_label(s) for s in rest) + ".",
                     sections=[])

    def on_plan_stage(self, ev, d):
        sol = d.get("solver") or {}
        st = d["stage"]
        titles = {"B1": "Stage B1: maximize the total P1 guarantee",
                  "B2": "Stage B2: maximize the total P2 guarantee (P1 locked)",
                  "B4": "Stage B4: minimize total reservations (every guarantee locked)"}
        if d.get("optimum") is None:
            summary = f"HiGHS could not solve this stage ({sol.get('message')})."
            if st == "B1":
                summary += " The planner returns an infeasible plan with zero guarantees."
            elif st == "B2":
                summary += " The B1 solution is kept."
            else:
                summary += " The previous solution is kept."
        elif st == "B4":
            summary = f"Minimum total reservation: {mb(d['optimum'])}."
        else:
            summary = f"Optimum: {mb(d['optimum'])} across {', '.join(d['flows']) or 'no flows'}; locked."
        sections = [("What it is doing", d.get("objective")),
                    ("Solver", f"status {sol.get('status')} ({sol.get('message')}); {sol.get('iterations')} iterations "
                               "reported. Internal solver steps are not visible.")]
        if st == "B4":
            sections.append(("Why", "Many reservation sets give the same guarantees; this stage picks one with the "
                                    "smallest total reserved bandwidth."))
        self.add(ev, group="protection", phase=f"Protection Planner · stage {st}", fidelity="stage",
                 title=titles.get(st, st), summary=summary, sections=sections)

    def on_plan_guarantees_chosen(self, ev, d):
        rows = [[fid, pname(self._plan.get("critical", {}).get(fid, {}).get("priority", "?")),
                 mb(self._plan.get("critical", {}).get(fid, {}).get("bandwidth")), mb(g)]
                for fid, g in d["guarantees"].items()]
        why_zero = []
        crit = self._plan.get("critical", {})
        for fid, g in d["guarantees"].items():
            if g > TOL:
                continue
            dead = [sc for sc, tids in (self._plan.get("survival", {}).get(fid) or {}).items() if not tids]
            if dead:
                why_zero.append(f"{fid}: in scenario “{scenario_label(dead[0])}” none of its tunnels survives, so "
                                f"Constraint 2 forces b[{fid}] ≤ 0")
            else:
                why_zero.append(f"{fid}: every scenario leaves it a tunnel, but shared capacity (after the locked "
                                "higher-priority guarantees) leaves no room")
        partial = [f"{fid}: {mb(g)} of {mb(crit.get(fid, {}).get('bandwidth'))}" for fid, g in d["guarantees"].items()
                   if TOL < g < (crit.get(fid, {}).get("bandwidth") or 0) - 1e-6]
        self.add(ev, group="protection", phase="Protection Planner · guarantees", fidelity="stage",
                 title="Per-flow guarantees chosen",
                 summary=", ".join(f"{k} {mb(v)}" for k, v in d["guarantees"].items()) + ".",
                 sections=[("Next", f"Taken from stage {d['from_stage']}; each b[f] is now fixed exactly."),
                           ("Why some are 0", "; ".join(why_zero) + "." if why_zero else ""),
                           ("Partial guarantees", ("; ".join(partial) + ": limited by shared capacity and the "
                                                   "survival constraints.") if partial else "")],
                 table={"title": "Guarantees", "columns": ["Flow", "Priority", "Requested", "Guarantee"], "rows": rows})

    def on_plan_result(self, ev, d):
        for key, t in (d.get("tunnels") or {}).items():
            self.tunnels[key] = t
        rows, paths = [], []
        reserved_per_link: dict[str, float] = {}
        for fid, res in d["reservations"].items():
            for tid, a in res.items():
                t = self.tunnel(fid, tid)
                rows.append([fid, tid, arrow(t["node_path"]) if t else tid, mb(a)])
                if t:
                    paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": "reserved", "flow": fid,
                                  "mbps": a, "label": f"{fid} {mb(a)} reserved"})
                    for lid in t["link_ids"]:
                        reserved_per_link[lid] = reserved_per_link.get(lid, 0.0) + a
        g = d["guarantees"]
        self.add(ev, group="protection", phase="Protection Planner · plan",
                 title="Protection plan (before verification)",
                 summary=(", ".join(f"{k} guaranteed {mb(v)}" for k, v in g.items() if v > TOL)
                          or "All guarantees are 0 Mbps") + f" · solver status {d['solver_status']}.",
                 sections=[("How to read it",
                            "A flow with two reserved tunnels of 10 Mbps keeps 10 Mbps when either one is cut: the "
                            "reservations are backups for each other, not 20 Mbps of traffic."),
                           ("Next", "The Guarantee Checker independently re-verifies every scenario.")],
                 tables=[{"title": "Reservations", "columns": ["Flow", "Tunnel", "Path", "Reserved"], "rows": rows},
                         {"title": "Reserved per link", "columns": ["Link", "Reserved", "Capacity"],
                          "rows": [[lid, mb(v), mb(self.capacity.get(lid))] for lid, v in sorted(reserved_per_link.items())]}
                         if reserved_per_link else None],
                 viz={"paths": paths})

    def on_check_prevalidation(self, ev, d):
        loads = d.get("reservation_link_loads") or {}
        rows = [[lid, mb(v), mb(d["capacity"].get(lid))] for lid, v in loads.items() if v > TOL]
        fails = d.get("failures") or []
        self.add(ev, group="checker", phase="Guarantee Checker · plan sanity",
                 title="Checker: is the plan itself valid?",
                 summary=("Reservations fit within capacity and every tunnel is a valid path." if not fails
                          else f"{plural(len(fails), 'problem')} found: " + "; ".join(fails[:3])),
                 sections=[("What it is doing", "Independent of the planner: checks solver status, scenario names, "
                                                "guarantee bounds, and that all reservations together respect link "
                                                "capacities using reconstructed paths."),
                           ("Next", f"Replay Reflex in each of the {len(d.get('scenarios') or [])} scenarios." if not fails else "")],
                 table={"title": "Reserved load per link", "columns": ["Link", "Reserved", "Capacity"], "rows": rows}
                 if rows else None)

    def on_check_scenario(self, ev, d):
        sc = d["scenario"]
        failed = d.get("failed_links") or []
        rows = []
        for fid, g in (d.get("guarantees") or {}).items():
            if g <= 1e-4:
                continue
            got = (d.get("delivered") or {}).get(fid, 0.0)
            rows.append([fid, mb(g), mb(got), "✓" if got >= g - (d.get("tolerance") or 1e-4) else "✗"])
        saved_loads, saved_failed = self.loads, self.failed
        self.loads = {lid: float(v) for lid, v in (d.get("link_loads") or {}).items()} or {lid: 0.0 for lid in self.links}
        self.failed = set(saved_failed) | set(failed)
        if not d.get("reflex_success"):
            summary = f"FAIL: Reflex could not recover ({d.get('reflex_message')})."
        elif d.get("violations"):
            summary = "FAIL: the Reflex allocation violates a constraint."
        elif not rows:
            summary = "PASS: no flow has a positive guarantee, so there is nothing to deliver in this scenario."
        else:
            summary = ("PASS: every guarantee is delivered." if d.get("passed") else "FAIL: a guarantee is not delivered.")
        self.add(ev, group="checker", phase="Guarantee Checker · scenarios", kind="check.scenario",
                 title=f"Checker · scenario “{scenario_label(sc)}”",
                 summary=summary,
                 sections=[("What it is doing",
                            "Replays solver-free Reflex in this scenario (no previous traffic) and validates the "
                            "result independently: valid paths, no failed link, shared capacity, then delivered ≥ "
                            "guarantee for every planned flow."),
                           ("Violations", "; ".join(d.get("violations") or []))],
                 table={"title": "Guarantee vs delivered", "columns": ["Flow", "Guarantee", "Delivered", ""], "rows": rows}
                 if rows else None,
                 viz={"paths": self.allocation_paths(d.get("allocations") or {}),
                      "links": {lid: "failing" for lid in failed}})
        self.loads, self.failed = saved_loads, saved_failed

    def on_check_result(self, ev, d):
        self.add(ev, group="checker", phase="Guarantee Checker · verdict",
                 title=f"Plan {'VERIFIED' if d['verified'] else 'NOT verified'}",
                 summary=f"{d['passed']} of {d['tested']} scenarios passed.",
                 sections=[("Meaning", "Reflex may use this plan after a covered failure." if d["verified"] else
                            "Reflex will not use this plan; failures fall back to PACAR."),
                           ("Failures", "; ".join((d.get("failures") or [])[:5]))])

    # ════════════════════════════════════════════════════════════════
    # Reflex
    # ════════════════════════════════════════════════════════════════
    def on_reflex_coverage(self, ev, d):
        if self._last_kind == "engine.coverage":
            return  # the controller's coverage step already explained this decision
        self.add(ev, group="reflex", phase="Reflex · coverage",
                 title="Reflex: is the failure set a covered scenario?",
                 summary=(f"Yes: “{scenario_label(d['scenario'])}”." if d["covered"] else "No: not covered."),
                 sections=[("Unavailable links", ", ".join(d["failed_links"]) or "none"),
                           ("Plan baseline", ", ".join(d["base_unavailable"]) or "none")])

    def on_reflex_abort(self, ev, d):
        reasons = {"not_covered": "the failure set is not a covered scenario",
                   "unverified": "the plan is not verified",
                   "invalid_or_guarantee_unmet": "the allocation failed validation or missed a guarantee"}
        self.add(ev, group="reflex", phase="Reflex", title="Reflex stops",
                 summary=f"No allocation: {reasons.get(d['reason'], d['reason'])}.", sections=[])

    def on_reflex_critical_flow(self, ev, d):
        fid = d["flow"]
        for tid, t in (d.get("tunnels") or {}).items():
            self.tunnels[f"{fid}|{tid}"] = t
        surv = {tid: a for tid, a in d["surviving"]}
        lost = {x["tunnel"]: x["reasons"] for x in d["lost"]}
        rows, paths = [], []
        for tid, a in d["reservations"].items():
            t = self.tunnel(fid, tid)
            got = d["allocation"].get(tid, 0.0)
            rows.append([tid, arrow(t["node_path"]) if t else tid, mb(a),
                         "survives" if tid in surv else "lost: " + "; ".join(lost.get(tid, [])), mb(got)])
            if t:
                paths.append({"nodes": t["node_path"], "links": t["link_ids"],
                              "role": "allocation" if got > TOL else "rejected", "flow": fid, "mbps": got,
                              "label": f"{fid} {mb(got)}" if got > TOL else f"{tid} lost"})
        if d["target"] > TOL:
            dec = (f"Allocate min(requested {mb(d['requested'])}, guarantee {mb(d['guaranteed'])}, surviving "
                   f"reservations {mb(d['S'])}) = {mb(d['target'])}, split in proportion to the surviving reservations.")
        elif d["guaranteed"] <= TOL:
            dec = "The verified guarantee is 0 Mbps, so nothing is allocated in this phase."
        else:
            dec = "No reserved tunnel survives, so nothing can be allocated from the plan."
        self.add(ev, group="reflex", phase="Reflex · guaranteed traffic",
                 title=f"{fid}: switch to its surviving reservations",
                 summary=dec,
                 sections=[("Demand", self.dlabel(fid)),
                           ("What it is doing", "Solver-free: Reflex only does arithmetic on the precomputed, verified "
                                                "plan. Critical (P1/P2) flows are handled first."),
                           ("Decision", dec)],
                 table={"title": "Reserved tunnels", "columns": ["Tunnel", "Path", "Reserved", "After failure",
                                                                 "Allocated"], "rows": rows} if rows else None,
                 viz={**self._endpoints_viz(fid), "paths": paths})

    def on_reflex_critical_loads(self, ev, d):
        self.loads = {lid: float(d["loads"].get(lid, 0.0)) for lid in self.links}
        rows = [[lid, mb(v), mb(d["capacity"].get(lid))] for lid, v in d["loads"].items() if v > TOL]
        self.add(ev, group="reflex", phase="Reflex · guaranteed traffic",
                 title="Guaranteed traffic placed",
                 summary=f"{mb(d['critical_recovered'])} of guaranteed traffic now occupies the links below.",
                 sections=[("Next", "Other surviving traffic is kept strictly by priority in the capacity that is left.")],
                 table={"title": "Load from guarantees", "columns": ["Link", "Load", "Capacity"], "rows": rows} if rows else None)

    def on_reflex_preserve_wanted(self, ev, d):
        fid = d["flow"]
        for tid, t in (d.get("tunnels") or {}).items():
            self.tunnels[f"{fid}|{tid}"] = t
        self._reflex_wanted.append(d)

    def _flush_wanted(self):
        if not self._reflex_wanted:
            return
        rows = []
        for w in self._reflex_wanted:
            for tid, bw in w["wanted"].items():
                t = self.tunnel(w["flow"], tid)
                rows.append([w["flow"], pname(w["priority"]), arrow(t["node_path"]) if t else tid, mb(bw), "kept (survives)"])
            for x in w["dropped"]:
                t = self.tunnel(w["flow"], x["tunnel"])
                rows.append([w["flow"], pname(w["priority"]), arrow(t["node_path"]) if t else x["tunnel"],
                             mb(x.get("rate")), "dropped: " + "; ".join(x["reasons"])])
        self.add(None, group="reflex", phase="Reflex · keep other traffic", kind="reflex.preserve_wanted",
                 title="Which previous traffic survived the failure?",
                 summary=(f"{plural(sum(len(w['wanted']) for w in self._reflex_wanted), 'previous tunnel allocation')} "
                          f"can be kept; {plural(sum(len(w['dropped']) for w in self._reflex_wanted), 'is', 'are')} "
                          "dropped.").replace(" 1 is dropped", " 1 is dropped").replace("; 0 are dropped", "; none is dropped"),
                 sections=[("What it is doing", "Each flow wants to keep its previous rate on tunnels that avoid the "
                                                "failed link (minus what the guarantee already gave it, never more "
                                                "than requested)."),
                           ("Not listed", "Flows already fully served by their guarantee want nothing more and are "
                                          "not listed.")],
                 table={"title": "Previous allocation", "columns": ["Flow", "Priority", "Tunnel path", "Rate", ""],
                        "rows": rows})
        self._reflex_wanted = []

    def on_reflex_preserve_class(self, ev, d):
        self._flush_wanted()
        p = d["priority"]
        squeezed = {lid: s for lid, s in d["link_scale"].items() if s < 1 - 1e-12}
        rows = [[lid, mb(d["room"].get(lid)), mb(d["load"].get(lid)), f"×{s:.3f}" if lid in squeezed else "×1 (fits)"]
                for lid, s in d["link_scale"].items()]
        granted_rows, paths = [], []
        for fid, ta in d["granted"].items():
            for tid, bw in ta.items():
                t = self.tunnel(fid, tid)
                want = (d["requested"].get(fid) or {}).get(tid, 0.0)
                granted_rows.append([fid, arrow(t["node_path"]) if t else tid, mb(want), mb(bw)])
                if t and bw > TOL:
                    paths.append({"nodes": t["node_path"], "links": t["link_ids"], "role": "allocation", "flow": fid,
                                  "mbps": bw, "label": f"{fid} {mb(bw)}"})
        self.loads = {lid: float(d["used"].get(lid, 0.0)) for lid in self.links}
        self.add(ev, group="reflex", phase=f"Reflex · keep P{p} traffic",
                 title=f"Keep {pname(p)} traffic in the remaining capacity",
                 summary=("Everything fits; previous rates are kept." if not squeezed else
                          "Scaled down where capacity is short: " + ", ".join(f"{lid} ×{s:.3f}" for lid, s in squeezed.items()) + "."),
                 sections=[("What it is doing", "For each link, if this class wants more than the room left by higher "
                                                "classes, every tunnel of the class through that link is scaled by "
                                                "room ÷ wanted (a tunnel takes its smallest factor). One pass, no solver."),
                           ("Order", "P1 and P2 above their guarantee first, then P3, P4, P5.")],
                 tables=[{"title": "Room per link", "columns": ["Link", "Room left", "Wanted", "Scale"], "rows": rows},
                         {"title": "Granted", "columns": ["Flow", "Tunnel path", "Wanted", "Granted"], "rows": granted_rows}],
                 viz={"paths": paths, "links": {lid: "bottleneck" for lid in squeezed}})

    def on_reflex_no_previous(self, ev, d):
        pass

    def on_reflex_validation(self, ev, d):
        self._flush_wanted()
        self.add(ev, group="reflex", phase="Reflex · validation",
                 title="Validate the recovery allocation",
                 summary=("Valid, and every guarantee is met." if d["valid"] and d["guarantees_met"]
                          else "INVALID or a guarantee is missed: " + "; ".join(d.get("violations") or [])),
                 sections=[("What it is doing", "The independent validator checks paths, failed links and shared "
                                                "capacity; Reflex also confirms each flow got at least its guarantee.")])

    def on_reflex_result(self, ev, d):
        self.add(ev, group="reflex", phase="Reflex · result",
                 title="Reflex result",
                 summary=(f"Critical traffic {mb(d['critical_recovered'])} ({mb(d['critical_guaranteed'])} from "
                          f"guarantees), other traffic {mb(d['normal_recovered'])}, unserved {mb(d['unserved'])}."),
                 sections=[("Timing", f"{ms(d.get('runtime_ms'))} of computation in this simulator. This is not "
                                      "measured network downtime.")],
                 viz={"paths": self.allocation_paths(d["allocations"])})

    # ════════════════════════════════════════════════════════════════
    # Install policy
    # ════════════════════════════════════════════════════════════════
    def on_policy_decision(self, ev, d):
        rule_text = {
            "stale_version": "The candidate was computed for an older network version, so it is rejected.",
            "candidate_invalid": "The candidate fails independent validation, so it is rejected.",
            "current_invalid": "The current allocation is invalid under the current failures, so any valid candidate "
                               "is installed.",
            "priority_reduced": f"The candidate would reduce P{d.get('deciding_priority')} traffic; higher priorities "
                                "are never sacrificed, so it is rejected.",
            "priority_improved": f"The candidate delivers more P{d.get('deciding_priority')} traffic (the highest "
                                 "class that differs), so it is installed.",
            "throughput_improved": "Every class is equal and the candidate delivers more in total (beyond the 1% "
                                   "churn threshold), so it is installed.",
            "churn_avoided": "Every class is equal and the total gain is within the 1% churn threshold, so the current "
                             "routes are kept to avoid needless route changes.",
        }
        cand, curr = d.get("candidate_by_priority") or {}, d.get("current_by_priority") or {}
        rows = [[f"P{p}", mb(curr.get(str(p))), mb(cand.get(str(p)))] for p in range(1, 6)
                if str(p) in cand or str(p) in curr]
        rows.append(["Total", mb(d.get("current_delivered")), mb(d.get("candidate_delivered"))])
        self.add(ev, group="policy", phase="Install policy",
                 title="Install policy: replace the current allocation with the LP's?",
                 summary=("INSTALL. " if d["install"] else "KEEP CURRENT. ") + rule_text.get(d["rule"], d.get("reason", "")),
                 sections=[("Rules (in order)", "1 stale version → reject; 2 invalid candidate → reject; 3 current "
                                                "allocation invalid → install; 4 compare P1…P5, first difference "
                                                "decides; 5 otherwise install only if total improves by > 1% of "
                                                "total demand."),
                           ("Decided by", d["rule"].replace("_", " ")),
                           ("Threshold", mb(d.get("threshold")) if d.get("threshold") is not None else "")],
                 table={"title": "Delivered per class", "columns": ["Class", "Current", "Candidate (LP)"], "rows": rows}
                 if cand or curr else None)


def build_steps(events: list[dict], context: dict) -> list[dict]:
    return NarrativeBuilder(context).build(events)
