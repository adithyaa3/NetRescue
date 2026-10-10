"""
Algorithm C: Reflex Recovery Engine
===================================
Provides solver-free recovery upon a covered link failure using the pre-computed,
verified Protection Plan:
1. every P1/P2 flow receives its verified guarantee on its surviving reserved
   tunnels (proportionally to the reservations);
2. surviving previous allocations are then kept strictly by priority (P1 and P2
   traffic above the guarantee, then P3, P4, P5), each class scaled down only on
   physical links where the capacity left by higher classes is short.
Enforces shared undirected capacity in both directions; the result is validated
before it is returned.
"""

from __future__ import annotations
import time
from typing import Optional, Any

from app.routing.validator import value, tunnel_errors, validate_allocation_independently
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
    network_version: Optional[int] = None,
) -> dict:
    """
    Executes Reflex Recovery.
    Returns:
    {
        "success": bool,
        "is_covered": bool,
        "allocations": dict[str, dict[str, float]], # flow_id -> {tunnel_id -> mbps}
        "network_version": int,
        "runtime_ms": float,
        "critical_recovered_mbps": float,
        "normal_recovered_mbps": float,
        "unserved_mbps": float,
        "message": str
    }
    """
    start_time = time.perf_counter()
    if network_version is None:
        network_version = protection_plan.topology_version

    failed_links = set(failed_links) | {lid for lid, link in links.items()
                                             if value(link, "status") in ("failed", "flapping")}
    # Check coverage relative to the plan baseline (links already down when it was
    # computed): at most one additional failed link, and that scenario must be covered.
    is_covered = protection_plan.scenario_for(failed_links) is not None

    if not is_covered:
        elapsed = (time.perf_counter() - start_time) * 1000
        return {
            "success": False,
            "is_covered": False,
            "allocations": {},
            "network_version": network_version,
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
            "network_version": network_version,
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
        p = value(demand, "priority")
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
            if t and t.survives_failure(failed_links) and not tunnel_errors(t, fid, demand, links, failed_links):
                surviving_tunnels.append((tid, a_val))

        S = sum(a for _, a in surviving_tunnels)
        target = min(req_bw, guaranteed, S)

        if S > 1e-12 and target > 1e-12:
            for tid, a_val in surviving_tunnels:
                alloc_val = target * (a_val / S)
                if alloc_val > 1e-12:
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

    # 3. Preserve surviving previous allocations, strictly by priority, inside the
    #    capacity left after the guaranteed critical allocations. Each class in turn
    #    (P1 and P2 traffic above its guarantee, then P3, P4, P5) keeps its previous
    #    rate on tunnels that survive the failure, scaled down per physical link
    #    (both directions share capacity) only where the remaining room is short.
    #    Solver-free: one pass per class.
    used: dict[str, float] = dict(critical_link_loads)
    normal_recovered = 0.0
    critical_preserved = 0.0
    if previous_allocations:
        by_class: dict[int, dict[str, dict[str, float]]] = {p: {} for p in range(1, 6)}
        for fid, demand in demands.items():
            p = value(demand, "priority")
            pval = getattr(p, "value", p) if p is not None else 4
            pval = pval if pval in by_class else 4
            req_bw = float(value(demand, "bandwidth", 0.0))
            already = allocations.get(fid, {})
            room_in_demand = max(0.0, req_bw - sum(already.values()))
            if room_in_demand <= 1e-12:
                continue
            wanted: dict[str, float] = {}
            for tid, bw in (previous_allocations.get(fid) or {}).items():
                t = tunnel_lookup.get((fid, tid))
                if not t or bw <= 1e-12 or not t.survives_failure(failed_links) \
                        or tunnel_errors(t, fid, demand, links, failed_links):
                    continue
                extra = bw - already.get(tid, 0.0)
                if extra > 1e-12:
                    wanted[tid] = extra
            total = sum(wanted.values())
            if total > 1e-12:
                scale = min(1.0, room_in_demand / total)
                by_class[pval][fid] = {tid: bw * scale for tid, bw in wanted.items()}

        for pval in range(1, 6):
            cls = by_class[pval]
            if not cls:
                continue
            cls_load: dict[str, float] = {}
            for fid, t_allocs in cls.items():
                for tid, bw in t_allocs.items():
                    for lid in tunnel_lookup[(fid, tid)].link_ids:
                        cls_load[lid] = cls_load.get(lid, 0.0) + bw
            link_scale: dict[str, float] = {}
            for lid, load in cls_load.items():
                cap = float(value(links.get(lid), "capacity", 0.0)) if lid in links else 0.0
                room = max(0.0, cap - used.get(lid, 0.0))
                link_scale[lid] = 1.0 if load <= room + 1e-12 else room / load
            for fid, t_allocs in cls.items():
                for tid, bw in t_allocs.items():
                    t = tunnel_lookup[(fid, tid)]
                    final_bw = bw * min((link_scale.get(lid, 1.0) for lid in t.link_ids), default=1.0)
                    if final_bw <= 1e-12:
                        continue
                    allocations[fid][tid] = allocations[fid].get(tid, 0.0) + final_bw
                    for lid in t.link_ids:
                        used[lid] = used.get(lid, 0.0) + final_bw
                    if pval <= 2:
                        critical_preserved += final_bw
                    else:
                        normal_recovered += final_bw
    critical_guaranteed = critical_recovered
    critical_recovered += critical_preserved

    check = validate_allocation_independently(allocations, demands, links,
        {t.id: t for ts in candidate_tunnels.values() for t in ts}, failed_links=failed_links)
    guarantees_met = all(sum(allocations.get(fid, {}).values()) >= g - 1e-8
                         for fid, g in protection_plan.per_flow_guaranteed.items())
    if not check.is_valid or not guarantees_met:
        return {"success": False, "is_covered": True, "allocations": {},
                "network_version": network_version,
                "runtime_ms": (time.perf_counter()-start_time)*1000,
                "critical_recovered_mbps": 0.0, "normal_recovered_mbps": 0.0,
                "unserved_mbps": sum(value(d, "bandwidth", 0) for d in demands.values()),
                "message": "Reflex allocation invalid or guarantee unmet: " + "; ".join(check.violations)}

    total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
    total_served = critical_recovered + normal_recovered
    unserved = max(0.0, total_req - total_served)

    elapsed = (time.perf_counter() - start_time) * 1000

    return {
        "success": True,
        "is_covered": True,
        "allocations": allocations,
        "network_version": network_version,
        "runtime_ms": elapsed,
        "critical_recovered_mbps": critical_recovered,
        "critical_guaranteed_mbps": critical_guaranteed,
        "normal_recovered_mbps": normal_recovered,
        "unserved_mbps": unserved,
        "message": f"Reflex recovery restored {critical_recovered:.1f} Mbps critical and {normal_recovered:.1f} Mbps normal traffic.",
    }
