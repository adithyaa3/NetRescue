"""
Algorithm C: Reflex Recovery Engine
===================================
Provides instantaneous O(1) guaranteed recovery for critical flows (P1, P2)
upon a covered link failure using the pre-computed, verified Protection Plan.
Preserves non-critical traffic (P3-P5) by scaling down allocations proportionally
on physical links where critical traffic leaves insufficient headroom.
Enforces shared undirected capacity in both directions.
"""

from __future__ import annotations
import time
from typing import Optional, Any

from app.models import Priority, LinkStatus
from app.routing.candidate_paths import CandidateTunnel
from app.routing.protection_planner import ProtectionPlan


def run_reflex_recovery(
    failed_links: set[str],
    protection_plan: ProtectionPlan,
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    previous_allocations: Optional[dict[str, dict[str, float]]] = None,
    is_verification_mode: bool = False,
) -> dict:
    """
    Executes Reflex Recovery.
    Returns:
    {
        "success": bool,
        "is_covered": bool,
        "allocations": dict[str, dict[str, float]], # flow_id -> {tunnel_id -> mbps}
        "runtime_ms": float,
        "critical_recovered_mbps": float,
        "normal_recovered_mbps": float,
        "unserved_mbps": float,
        "message": str
    }
    """
    start_time = time.perf_counter()

    # Check coverage
    # A single link failure is covered if covered_scenarios has f"fail_{lid}" or lid in covered_scenarios
    if len(failed_links) == 0:
        is_covered = "no_failure" in protection_plan.covered_scenarios
    elif len(failed_links) == 1:
        lid = next(iter(failed_links))
        is_covered = f"fail_{lid}" in protection_plan.covered_scenarios or lid in protection_plan.covered_scenarios
    else:
        # Multiple failures are uncovered by default single-link protection
        is_covered = False

    if not is_covered:
        elapsed = (time.perf_counter() - start_time) * 1000
        return {
            "success": False,
            "is_covered": False,
            "allocations": {},
            "runtime_ms": elapsed,
            "critical_recovered_mbps": 0.0,
            "normal_recovered_mbps": 0.0,
            "unserved_mbps": 0.0,
            "message": f"Failure set {list(failed_links)} is not covered by the protection plan.",
        }

    if not is_verification_mode and not protection_plan.verified:
        elapsed = (time.perf_counter() - start_time) * 1000
        return {
            "success": False,
            "is_covered": True,
            "allocations": {},
            "runtime_ms": elapsed,
            "critical_recovered_mbps": 0.0,
            "normal_recovered_mbps": 0.0,
            "unserved_mbps": 0.0,
            "message": "Protection plan has not passed independent guarantee verification.",
        }

    # Tunnel map for fast lookup: (flow_id, tunnel_id) -> CandidateTunnel
    tunnel_lookup: dict[tuple[str, str], CandidateTunnel] = {}
    for fid, t_list in candidate_tunnels.items():
        for t in t_list:
            tunnel_lookup[(fid, t.id)] = t

    allocations: dict[str, dict[str, float]] = {fid: {} for fid in demands}
    critical_recovered = 0.0

    # 1. Critical allocation (P1 & P2)
    for fid, demand in demands.items():
        p = getattr(demand, "priority", None)
        pval = getattr(p, "value", p) if p is not None else 4
        is_critical = pval in (Priority.P1_EMERGENCY.value, Priority.P2_ESSENTIAL.value, 1, 2)
        if not is_critical:
            continue

        req_bw = float(getattr(demand, "bandwidth", 0.0) if not isinstance(demand, dict) else demand.get("bandwidth", 0.0))
        guaranteed = float(protection_plan.per_flow_guaranteed.get(fid, 0.0))
        reservations = protection_plan.per_tunnel_reservations.get(fid, {})

        # Find surviving reserved tunnels
        surviving_tunnels: list[tuple[str, float]] = []
        for tid, a_val in reservations.items():
            t = tunnel_lookup.get((fid, tid))
            if t and t.survives_failure(failed_links):
                surviving_tunnels.append((tid, a_val))

        S = sum(a for _, a in surviving_tunnels)
        target = min(req_bw, guaranteed, S)

        if S > 1e-6 and target > 1e-6:
            for tid, a_val in surviving_tunnels:
                alloc_val = target * (a_val / S)
                if alloc_val > 1e-5:
                    allocations[fid][tid] = alloc_val
            critical_recovered += target

    # 2. Compute critical load on every physical link (UNDIRECTED: both directions combined)
    critical_link_loads: dict[str, float] = {lid: 0.0 for lid in links}
    for fid, t_allocs in allocations.items():
        for tid, bw in t_allocs.items():
            t = tunnel_lookup.get((fid, tid))
            if t:
                for lid in t.link_ids:
                    critical_link_loads[lid] = critical_link_loads.get(lid, 0.0) + bw

    # 3. Normal traffic preservation (P3, P4, P5)
    normal_allocations: dict[str, dict[str, float]] = {}
    if previous_allocations:
        for fid, demand in demands.items():
            p = getattr(demand, "priority", None)
            pval = getattr(p, "value", p) if p is not None else 4
            is_critical = pval in (Priority.P1_EMERGENCY.value, Priority.P2_ESSENTIAL.value, 1, 2)
            if is_critical:
                continue

            prev_tunnels = previous_allocations.get(fid, {})
            flow_norm: dict[str, float] = {}
            for tid, bw in prev_tunnels.items():
                t = tunnel_lookup.get((fid, tid))
                # Only keep surviving tunnels
                if t and t.survives_failure(failed_links) and bw > 1e-5:
                    flow_norm[tid] = bw
            if flow_norm:
                normal_allocations[fid] = flow_norm

    # Check headroom on each physical link and scale down normal traffic if congested
    # room[e] = max(0, capacity[e] - critical_link_loads[e])
    # Total normal load on link e
    normal_link_loads: dict[str, float] = {lid: 0.0 for lid in links}
    for fid, t_allocs in normal_allocations.items():
        for tid, bw in t_allocs.items():
            t = tunnel_lookup.get((fid, tid))
            if t:
                for lid in t.link_ids:
                    normal_link_loads[lid] = normal_link_loads.get(lid, 0.0) + bw

    # Scaling factor per link: min(1.0, room[e] / normal_load[e])
    link_scale: dict[str, float] = {}
    for lid, link in links.items():
        cap = float(getattr(link, "capacity", 0.0) if not isinstance(link, dict) else link.get("capacity", 0.0))
        room = max(0.0, cap - critical_link_loads.get(lid, 0.0))
        n_load = normal_link_loads.get(lid, 0.0)
        if n_load > room + 1e-5 and n_load > 0:
            link_scale[lid] = room / n_load
        else:
            link_scale[lid] = 1.0

    # Apply scaling to normal allocations (each tunnel takes the min scale across traversed links)
    normal_recovered = 0.0
    for fid, t_allocs in normal_allocations.items():
        for tid, bw in t_allocs.items():
            t = tunnel_lookup.get((fid, tid))
            if not t:
                continue
            scale = min((link_scale.get(lid, 1.0) for lid in t.link_ids), default=1.0)
            final_bw = bw * scale
            if final_bw > 1e-5:
                allocations[fid][tid] = final_bw
                normal_recovered += final_bw

    total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
    total_served = critical_recovered + normal_recovered
    unserved = max(0.0, total_req - total_served)

    elapsed = (time.perf_counter() - start_time) * 1000

    return {
        "success": True,
        "is_covered": True,
        "allocations": allocations,
        "runtime_ms": elapsed,
        "critical_recovered_mbps": critical_recovered,
        "normal_recovered_mbps": normal_recovered,
        "unserved_mbps": unserved,
        "message": f"Reflex recovery restored {critical_recovered:.1f} Mbps critical and {normal_recovered:.1f} Mbps normal traffic.",
    }
