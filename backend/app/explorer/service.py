"""
Algorithm Explorer: service layer
=================================
Read-only with respect to the live network. Every trace runs the real controller
code on a private deep copy of a frozen snapshot; the live ``SimulationState`` is
only read (under its lock) when a session snapshot is taken.

Sessions
    A session freezes the live network at one moment. All traces and comparisons
    of a session use copies of that same snapshot, so algorithms are compared on
    identical topology, demands, link states, plan and installed allocation.

Verification
    Each traced run is repeated untraced on another copy of the snapshot. The two
    outcomes must be identical; any difference is reported, never hidden.

Live-action replay
    ``capture_before`` / ``record_after`` (called by the live API endpoints) keep a
    snapshot taken immediately before the last controller action together with
    the action's outcome. ``replay_last_action`` re-runs that action with tracing
    on a copy and checks the result equals what happened live.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from typing import Any, Optional

from app.models import DemandCreate
from app.state import SimulationState
from app.simulation import engine
from app.analysis.metrics import compute_metrics, delivered_bandwidth
from app.routing.allocation import validate_allocation
from app.explorer.trace import TraceRecorder, json_safe
from app.explorer.narrative import build_steps, PRIORITY_NAMES
from app.explorer.signatures import scenario_fingerprint, outcome_signature, diff_signatures

TRACE_FORMAT_VERSION = 1
MAX_SESSIONS = 12
MAX_TRACES_PER_SESSION = 24  # bounded memory: least recently used traces are recomputed on demand

ALGORITHMS = {
    "baseline": {
        "label": "Baseline Dijkstra",
        "short": "Dijkstra",
        "trace_level": "full",
        "trace_level_label": "Full step trace",
        "needs_failure": False,
        "description": "Capacity-constrained shortest path: demands in priority order, each on its single "
                       "minimum-latency path with spare capacity, taking what the bottleneck allows.",
        "source": "backend/app/routing/baseline.py · run_baseline",
        "used_by": "Simulation with algorithm=baseline; Algorithm Comparison.",
        "fidelity_note": "Every decision of run_baseline is recorded. Dijkstra's internal node expansion is "
                         "performed by NetworkX and is not shown; the explorer shows the path it returned.",
    },
    "pacar": {
        "label": "PACAR",
        "short": "PACAR",
        "trace_level": "full",
        "trace_level_label": "Full step trace",
        "needs_failure": False,
        "description": "Priority-Aware Capacity-Adaptive Routing: per priority group, score up to 5 shortest "
                       "paths, pick the lowest score with spare capacity, then share capacity by progressive filling.",
        "source": "backend/app/routing/pacar.py · run_pacar",
        "used_by": "Start Simulation (default); uncovered-failure fallback; multi-link failures; emergency demands.",
        "fidelity_note": "Every candidate, score term, fallback search and filling round of run_pacar is recorded.",
    },
    "reactive_lp": {
        "label": "Reactive LP",
        "short": "LP",
        "trace_level": "stage",
        "trace_level_label": "Stage trace",
        "needs_failure": False,
        "description": "Lexicographic multicommodity-flow linear program over candidate tunnels: maximize P1, "
                       "lock it, then P2 … P5, then minimize latency (HiGHS).",
        "source": "backend/app/routing/reactive_lp.py · solve_reactive_multicommodity_lp",
        "used_by": "Simulation with algorithm=reactive_lp; optimization after a failure; link restore.",
        "fidelity_note": "The model, each stage's objective, optimal value and solver status are recorded from the "
                         "real run. HiGHS's internal iterations are not visible and are never invented.",
    },
    "protection": {
        "label": "Protection Planner + Checker",
        "short": "Protection",
        "trace_level": "stage",
        "trace_level_label": "Stage trace + full checker",
        "needs_failure": False,
        "description": "Proactive LP that reserves bandwidth so each P1/P2 flow keeps a guaranteed rate under "
                       "every single-link failure; an independent checker replays every scenario.",
        "source": "backend/app/routing/protection_planner.py · guarantee_checker.py",
        "used_by": "Every simulation step; Recompute Plan; link restore.",
        "fidelity_note": "Scenarios, survival constraints, stage optima and the plan are recorded from the real LP "
                         "run (solver internals not visible). Every Guarantee Checker scenario is recorded in full.",
    },
    "recovery": {
        "label": "Failure Recovery (Reflex)",
        "short": "Recovery",
        "trace_level": "mixed",
        "trace_level_label": "Full (Reflex, policy) + stage (LP)",
        "needs_failure": True,
        "description": "What the controller does when a link fails: Reflex on the verified plan (solver-free) "
                       "or PACAR fallback, then the Reactive LP under the install policy.",
        "source": "backend/app/simulation/engine.py · handle_link_failure (+ reflex.py, install_policy.py)",
        "used_by": "Fail Link on the dashboard; What-If analysis (single link).",
        "fidelity_note": "Reflex, the coverage decision, PACAR fallback and the install policy are recorded in full; "
                         "the Reactive LP at stage level.",
    },
}

ROUTING_ALGORITHMS = ("baseline", "pacar", "reactive_lp")


class ExplorerError(ValueError):
    """A request the explorer cannot satisfy (unknown algorithm, invalid link, …)."""


class SessionNotFound(KeyError):
    pass


# ── Scenario description ──────────────────────────────────────────────

def describe_scenario(state: SimulationState) -> dict:
    plan = state.protection_plan
    return {
        "nodes": [{"id": n.id, "name": n.name, "node_type": n.node_type.value, "x": n.x, "y": n.y,
                   "service_label": n.service_label} for n in state.nodes.values()],
        "links": [{"id": l.id, "source": l.source, "target": l.target, "capacity": l.capacity,
                   "latency": l.latency, "status": l.status.value, "allocated_load": l.allocated_load,
                   "utilization": l.utilization} for l in state.links.values()],
        "demands": [{"id": d.id, "source": d.source, "destination": d.destination, "bandwidth": d.bandwidth,
                     "priority": d.priority.value, "category": d.category.value, "max_latency": d.max_latency,
                     "status": d.status.value, "allocated": delivered_bandwidth(state, d)}
                    for d in state.demands.values()],
        "plan": None if plan is None else {
            "plan_id": plan.plan_id, "verified": plan.verified, "solver_status": plan.solver_status,
            "guarantees": plan.per_flow_guaranteed, "scenarios": len(plan.covered_scenarios),
            "base_unavailable": plan.base_unavailable,
            "current_scenario": plan.scenario_for({lid for lid in state.links if not state.is_link_usable(lid)}),
        },
        "unavailable_links": sorted(lid for lid in state.links if not state.is_link_usable(lid)),
        "version": state.version,
        "step": state.step,
    }


def _narrative_context(state: SimulationState) -> dict:
    return {
        "links": {l.id: {"source": l.source, "target": l.target, "capacity": l.capacity, "latency": l.latency}
                  for l in state.links.values()},
        "demands": {d.id: {"source": d.source, "destination": d.destination, "bandwidth": d.bandwidth,
                           "priority": d.priority.value, "max_latency": d.max_latency}
                    for d in state.demands.values()},
        "initial_loads": {l.id: l.allocated_load for l in state.links.values()},
        "initial_failed": [lid for lid in state.links if not state.is_link_usable(lid)],
    }


def summarize_result(state: SimulationState) -> dict:
    """Measured outcome held by ``state`` (after an algorithm ran on it)."""
    metrics = compute_metrics(state)
    demands = []
    for d in sorted(state.demands.values(), key=lambda d: (d.priority.value, d.id)):
        demands.append({
            "id": d.id, "source": d.source, "destination": d.destination, "priority": d.priority.value,
            "requested": d.bandwidth, "delivered": delivered_bandwidth(state, d),
            "status": d.status.value, "route": d.route,
            "tunnels": [{"tunnel_id": t["tunnel_id"], "node_path": t["node_path"], "bandwidth": t["bandwidth"]}
                        for t in d.tunnels],
        })
    links = [{"id": l.id, "source": l.source, "target": l.target, "capacity": l.capacity,
              "load": l.allocated_load, "utilization": l.utilization, "status": l.status.value}
             for l in state.links.values()]
    plan = state.protection_plan
    status_counts: dict[str, int] = {}
    for d in state.demands.values():
        status_counts[d.status.value] = status_counts.get(d.status.value, 0) + 1
    return {
        "metrics": {k: metrics[k] for k in (
            "total_requested", "total_delivered", "total_unserved", "critical_requested", "critical_delivered",
            "critical_delivery_pct", "general_requested", "general_delivered", "general_delivery_pct",
            "failed_links", "disconnected_demands", "active_demands", "max_link_utilization",
            "avg_link_utilization", "avg_latency")},
        "per_priority": metrics["per_priority"],
        "status_counts": status_counts,
        "demands": demands,
        "links": links,
        "violations": validate_allocation(state),
        "plan": None if plan is None else {
            "verified": plan.verified, "solver_status": plan.solver_status,
            "guarantees": plan.per_flow_guaranteed, "reservations": plan.per_tunnel_reservations,
            "scenarios": len(plan.covered_scenarios), "failures": plan.verification_failures,
        },
    }


# ── Running one traced action ─────────────────────────────────────────

def _run_action(state: SimulationState, algorithm: str, failed_link: Optional[str], trace) -> dict:
    """Execute the real controller code for ``algorithm`` on ``state`` (a private copy)."""
    if algorithm in ROUTING_ALGORITHMS:
        if failed_link:
            state.fail_link(failed_link)  # hypothetical outage on the private copy
            if trace is not None:
                trace.set_phase("controller")
                trace.emit("explorer.hypothetical_failure", link_id=failed_link)
        return engine.simulate(state, algorithm, trace=trace)
    if algorithm == "protection":
        if trace is not None:
            trace.set_phase("protection_planner")
        engine.refresh_candidate_tunnels(state, trace=trace)
        if trace is not None:
            trace.set_phase("protection_planner")
        engine.update_simulation_protection_plan(state, trace=trace)
        return {"plan": None if state.protection_plan is None else state.protection_plan.to_dict()}
    if algorithm == "recovery":
        return engine.handle_link_failure(state, failed_link, trace=trace)
    raise ExplorerError(f"Unknown algorithm '{algorithm}'")


def _validate_request(snapshot: SimulationState, algorithm: str, failed_link: Optional[str]) -> None:
    if algorithm not in ALGORITHMS:
        raise ExplorerError(f"Unknown algorithm '{algorithm}'. Supported: {', '.join(ALGORITHMS)}")
    if failed_link is not None and failed_link not in snapshot.links:
        raise ExplorerError(f"Link '{failed_link}' does not exist in this snapshot")
    if algorithm == "recovery":
        if not failed_link:
            raise ExplorerError("Failure Recovery needs a link to fail")
        status = snapshot.links[failed_link].status.value
        if status != "active":
            raise ExplorerError(f"Link '{failed_link}' is {status} in this snapshot; choose an active link")
    elif failed_link is not None and algorithm in ROUTING_ALGORITHMS:
        if not snapshot.is_link_usable(failed_link):
            raise ExplorerError(f"Link '{failed_link}' is already unavailable in this snapshot")
    elif failed_link is not None and algorithm == "protection":
        pass  # the planner runs on the pre-failure network; the link is only highlighted


def trace_on_snapshot(snapshot: SimulationState, algorithm: str, failed_link: Optional[str] = None,
                      verify: bool = True) -> dict:
    """Trace ``algorithm`` on a private copy of ``snapshot`` (never mutated)."""
    _validate_request(snapshot, algorithm, failed_link)
    run_failed_link = failed_link if algorithm != "protection" else None
    work = snapshot.snapshot()
    context = _narrative_context(work)
    recorder = TraceRecorder()
    t0 = time.perf_counter()
    response = _run_action(work, algorithm, run_failed_link, recorder)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    steps = build_steps(recorder.events, context)

    verification = {"untraced_rerun_identical": None, "differences": [], "recorder_errors": list(recorder.errors),
                    "truncated": recorder.truncated}
    if verify:
        reference = snapshot.snapshot()
        _run_action(reference, algorithm, run_failed_link, None)
        diffs = diff_signatures(outcome_signature(work), outcome_signature(reference))
        verification["untraced_rerun_identical"] = not diffs
        verification["differences"] = diffs

    # Does the traced result equal what the live network had installed when the
    # snapshot was taken? Meaningful only for routing algorithms without a
    # hypothetical failure (routing does not depend on the previous allocation).
    matches_live = None
    if algorithm in ROUTING_ALGORITHMS and not failed_link:
        before = outcome_signature(snapshot)
        after = outcome_signature(work)
        matches_live = before["allocations"] == after["allocations"] and before["demands"] == after["demands"]
    verification["matches_live_installed"] = matches_live

    meta = ALGORITHMS[algorithm]
    return json_safe({
        "trace_version": TRACE_FORMAT_VERSION,
        "algorithm": algorithm,
        "algorithm_label": meta["label"],
        "trace_level": meta["trace_level"],
        "trace_level_label": meta["trace_level_label"],
        "fidelity_note": meta["fidelity_note"],
        "failed_link": failed_link,
        "steps": steps,
        "raw_event_count": len(recorder.events),
        "events": recorder.events,
        "result": summarize_result(work),
        "response": {k: v for k, v in (response or {}).items() if k not in ("congestion_warnings",)},
        "verification": verification,
        "computation_ms": elapsed_ms,
    })


# ── Sessions ──────────────────────────────────────────────────────────

class ExplorerSession:
    def __init__(self, snapshot: SimulationState, fingerprint: str):
        self.id = uuid.uuid4().hex[:12]
        self.created_at = time.time()
        self.snapshot = snapshot
        self.fingerprint = fingerprint
        self.scenario = describe_scenario(snapshot)
        self._cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._lock = threading.Lock()

    def summary(self) -> dict:
        return {"session_id": self.id, "created_at": self.created_at, "fingerprint": self.fingerprint,
                "scenario": json_safe(self.scenario),
                "algorithms": [{"id": k, **{f: v for f, v in m.items()}} for k, m in ALGORITHMS.items()]}

    def trace(self, algorithm: str, failed_link: Optional[str]) -> dict:
        key = (algorithm, failed_link or None)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
        if cached is not None:
            return cached
        result = trace_on_snapshot(self.snapshot, algorithm, failed_link or None)
        result["session_id"] = self.id
        result["fingerprint"] = self.fingerprint
        with self._lock:
            self._cache[key] = result
            while len(self._cache) > MAX_TRACES_PER_SESSION:
                self._cache.popitem(last=False)
        return result

    def compare(self, failed_link: Optional[str]) -> dict:
        rows = []
        for alg in ROUTING_ALGORITHMS:
            tr = self.trace(alg, failed_link)
            m = tr["result"]["metrics"]
            rows.append({
                "algorithm": alg, "label": ALGORITHMS[alg]["label"],
                "algorithm_used": (tr.get("response") or {}).get("algorithm_used", alg),
                "metrics": m, "per_priority": tr["result"]["per_priority"],
                "status_counts": tr["result"]["status_counts"],
                "violations": tr["result"]["violations"],
                "computation_ms": (tr.get("response") or {}).get("computation_time_ms"),
                "verified_identical": tr["verification"]["untraced_rerun_identical"],
                "demands": [{"id": d["id"], "priority": d["priority"], "requested": d["requested"],
                             "delivered": d["delivered"], "status": d["status"]} for d in tr["result"]["demands"]],
            })
        return {"session_id": self.id, "fingerprint": self.fingerprint, "failed_link": failed_link,
                "basis": "Each column is the controller's real simulation step with that routing algorithm, run on "
                         "an identical private copy of this session's snapshot"
                         + (f" with {failed_link} failed" if failed_link else "") + ".",
                "rows": rows}


_sessions: "OrderedDict[str, ExplorerSession]" = OrderedDict()
_sessions_lock = threading.Lock()


def create_session(live_state: SimulationState) -> ExplorerSession:
    with live_state.lock():
        snap = live_state.snapshot()
        fp = scenario_fingerprint(live_state)
    session = ExplorerSession(snap, fp)
    with _sessions_lock:
        _sessions[session.id] = session
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
    return session


def get_session(session_id: str) -> ExplorerSession:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        _sessions.move_to_end(session_id)
        return session


def live_status(live_state: SimulationState) -> dict:
    with live_state.lock():
        fp = scenario_fingerprint(live_state)
        version, step = live_state.version, live_state.step
        nodes, links, demands = len(live_state.nodes), len(live_state.links), len(live_state.demands)
    last = last_action_summary(live_state)
    return {"fingerprint": fp, "version": version, "step": step, "nodes": nodes, "links": links,
            "demands": demands, "last_action": last}


# ── Live-action capture and replay ────────────────────────────────────

_live_actions: "weakref.WeakKeyDictionary[SimulationState, dict]" = weakref.WeakKeyDictionary()
_live_lock = threading.Lock()

ACTION_LABELS = {
    "simulate": "Simulation step",
    "link_failure": "Link failure",
    "link_restore": "Link restore",
    "batch_failure": "Multi-link failure",
    "emergency_demand": "Emergency demand",
}


def capture_before(state: SimulationState) -> Optional[SimulationState]:
    """Snapshot taken by a live endpoint right before a controller action. Never raises."""
    try:
        return state.snapshot()
    except Exception:
        return None


def record_after(state: SimulationState, pre: Optional[SimulationState], action: str, params: dict,
                 response: Any) -> None:
    """Remember the last live controller action and its outcome. Never raises."""
    try:
        if pre is None or not isinstance(response, dict) or response.get("changed") is False:
            return
        record = {
            "pre": pre,
            "action": action,
            "params": copy.deepcopy(params),
            "outcome": outcome_signature(state),
            "step": state.step,
            "version": state.version,
            "fingerprint_after": scenario_fingerprint(state),
            "at": time.time(),
            "response": json_safe({k: v for k, v in response.items() if k != "congestion_warnings"}),
        }
        with _live_lock:
            _live_actions[state] = record
    except Exception:
        pass


def last_action_summary(state: SimulationState) -> Optional[dict]:
    with _live_lock:
        rec = _live_actions.get(state)
    if rec is None:
        return None
    return {"action": rec["action"], "label": ACTION_LABELS.get(rec["action"], rec["action"]),
            "params": rec["params"], "step": rec["step"], "version": rec["version"], "at": rec["at"],
            "fingerprint_after": rec["fingerprint_after"], "response": rec["response"]}


def _replay(state: SimulationState, action: str, params: dict, trace) -> dict:
    if action == "simulate":
        return engine.simulate(state, params.get("algorithm", "pacar"), trace=trace)
    if action == "link_failure":
        return engine.handle_link_failure(state, params["link_id"], trace=trace)
    if action == "link_restore":
        return engine.handle_link_restore(state, params["link_id"], trace=trace)
    if action == "batch_failure":
        return engine.handle_batch_failures(state, params["link_ids"], trace=trace)
    if action == "emergency_demand":
        return engine.add_emergency_demand(state, DemandCreate(**params["demand"]), trace=trace)
    raise ExplorerError(f"Unknown action '{action}'")


def replay_last_action(live_state: SimulationState) -> dict:
    with _live_lock:
        rec = _live_actions.get(live_state)
    if rec is None:
        raise ExplorerError("No controller action has been recorded since the server started")
    work = rec["pre"].snapshot()
    context = _narrative_context(work)
    recorder = TraceRecorder()
    t0 = time.perf_counter()
    response = _replay(work, rec["action"], rec["params"], recorder)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    steps = build_steps(recorder.events, context)
    diffs = diff_signatures(outcome_signature(work), rec["outcome"])
    with live_state.lock():
        current_fp = scenario_fingerprint(live_state)
    algorithm = {"simulate": rec["params"].get("algorithm", "pacar"), "link_failure": "recovery",
                 "link_restore": "restore", "batch_failure": "pacar", "emergency_demand": "pacar"}[rec["action"]]
    label = ACTION_LABELS.get(rec["action"], rec["action"])
    detail = rec["params"].get("link_id") or ", ".join(rec["params"].get("link_ids", [])) or \
        rec["params"].get("algorithm") or (rec["params"].get("demand") or {}).get("id", "")
    return json_safe({
        "trace_version": TRACE_FORMAT_VERSION,
        "source": "live_replay",
        "algorithm": algorithm,
        "algorithm_label": f"Last live action: {label}" + (f" ({detail})" if detail else ""),
        "trace_level": "mixed",
        "trace_level_label": "Replay of the live action",
        "fidelity_note": "Replayed from a snapshot taken immediately before the live action, with the same "
                         "controller code; the outcome is compared with what happened live.",
        "failed_link": rec["params"].get("link_id"),
        "steps": steps,
        "raw_event_count": len(recorder.events),
        "events": recorder.events,
        "result": summarize_result(work),
        "response": {k: v for k, v in (response or {}).items() if k != "congestion_warnings"},
        "verification": {"replay_identical_to_live": not diffs, "differences": diffs,
                         "recorder_errors": list(recorder.errors), "truncated": recorder.truncated,
                         "live_changed_since": current_fp != rec["fingerprint_after"]},
        "live_action": last_action_summary(live_state),
        "scenario": describe_scenario(rec["pre"]),
        "computation_ms": elapsed_ms,
    })
