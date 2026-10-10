"""
Algorithm Explorer: scenario fingerprints and outcome signatures
================================================================
``scenario_fingerprint`` identifies the routing-relevant content of a network
(topology, link states, demands, installed allocation, protection plan, controller
step). Node positions and names are excluded: they never affect routing.

``outcome_signature`` captures what an algorithm run produced on a state, so two
runs (traced and untraced, or a live action and its replay) can be compared
exactly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.state import SimulationState


def _plan_part(state: SimulationState) -> Any:
    plan = state.protection_plan
    if plan is None:
        return None
    return {
        "plan_id": plan.plan_id,
        "verified": plan.verified,
        "guarantees": {k: plan.per_flow_guaranteed[k] for k in sorted(plan.per_flow_guaranteed)},
        "base_unavailable": sorted(plan.base_unavailable),
    }


def scenario_fingerprint(state: SimulationState) -> str:
    content = {
        "nodes": sorted(state.nodes),
        "links": sorted([l.id, l.source, l.target, l.capacity, l.latency, l.status.value,
                         l.flap_count, l.last_flap_step] for l in state.links.values()),
        "demands": sorted([d.id, d.source, d.destination, d.bandwidth, d.priority.value,
                           d.max_latency, d.status.value] for d in state.demands.values()),
        "allocations": {fid: {tid: rate for tid, rate in sorted(alloc.items())}
                        for fid, alloc in sorted(state.active_allocations.items())},
        "plan": _plan_part(state),
        "step": state.step,
    }
    raw = json.dumps(content, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _tunnel_paths(state: SimulationState, fid: str) -> dict:
    return {t.id: list(t.node_path) for t in state.candidate_tunnels.get(fid, [])}


def outcome_signature(state: SimulationState) -> dict:
    """Exact, comparable description of the routing outcome held by ``state``.

    Allocations are keyed by physical path (not tunnel ID) so two runs that chose
    the same paths compare equal even if tunnel numbering differed.
    """
    allocations = {}
    for fid in sorted(state.active_allocations):
        paths = _tunnel_paths(state, fid)
        allocations[fid] = sorted(
            ["→".join(paths.get(tid, [f"?{tid}"])), rate]
            for tid, rate in state.active_allocations[fid].items())
    plan = state.protection_plan
    return {
        "demands": {d.id: [d.status.value, d.allocated_bandwidth, d.unserved_bandwidth,
                           list(d.route) if d.route else None]
                    for d in sorted(state.demands.values(), key=lambda d: d.id)},
        "links": {l.id: [l.status.value, l.allocated_load]
                  for l in sorted(state.links.values(), key=lambda l: l.id)},
        "allocations": allocations,
        "plan": None if plan is None else {
            "verified": plan.verified,
            "solver_status": plan.solver_status,
            "guarantees": {k: plan.per_flow_guaranteed[k] for k in sorted(plan.per_flow_guaranteed)},
            "reservations": {fid: sorted(["→".join(_tunnel_paths(state, fid).get(tid, [f"?{tid}"])), a]
                                         for tid, a in res.items())
                             for fid, res in sorted(plan.per_tunnel_reservations.items())},
            "scenarios": list(plan.covered_scenarios),
        },
        "step": state.step,
    }


def diff_signatures(a: dict, b: dict, path: str = "") -> list[str]:
    """Human-readable differences between two signatures (empty when identical)."""
    diffs: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a:
                diffs.append(f"{path}{key}: only in second")
            elif key not in b:
                diffs.append(f"{path}{key}: only in first")
            else:
                diffs.extend(diff_signatures(a[key], b[key], f"{path}{key}."))
    elif a != b:
        diffs.append(f"{path.rstrip('.')}: {a!r} != {b!r}")
    return diffs[:50]
