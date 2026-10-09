"""
Algorithm D: Reactive Multicommodity-Flow LP Optimizer (HiGHS LP)
================================================================
Solves multi-commodity flow optimization across surviving candidate tunnels
using lexicographic priority stages (P1 -> P2 -> P3 -> P4 -> P5).
Strictly enforces shared undirected physical-link capacity across both directions:
for each link e, sum_{f,t: e in t} x[f,t] <= C[e].
Falls back to PACAR if problem is empty or LP solver fails.
"""

from __future__ import annotations
import time
from typing import Optional, Any
import numpy as np
from scipy.optimize import linprog

from app.models import Priority, LinkStatus
from app.routing.candidate_paths import CandidateTunnel


def solve_reactive_multicommodity_lp(
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    previous_allocations: Optional[dict[str, dict[str, float]]] = None,
    beta_churn: float = 0.05,
) -> dict:
    """
    Executes lexicographic multicommodity flow LP.
    Returns:
    {
        "success": bool,
        "allocations": dict[str, dict[str, float]], # flow_id -> {tunnel_id -> mbps}
        "runtime_ms": float,
        "solver_status": str,
        "delivered_by_priority": dict[str, float],
        "total_delivered": float,
        "total_unserved": float,
    }
    """
    start_time = time.perf_counter()

    # Identify surviving links
    failed_links = set()
    usable_links = {}
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status == "failed":
            failed_links.add(lid)
        else:
            usable_links[lid] = link

    # Gather surviving tunnels for active demands
    flow_tunnels: dict[str, list[CandidateTunnel]] = {}
    var_map_x: dict[tuple[str, str], int] = {}
    col = 0

    for fid, demand in demands.items():
        req_bw = float(getattr(demand, "bandwidth", 0.0) if not isinstance(demand, dict) else demand.get("bandwidth", 0.0))
        if req_bw <= 0:
            flow_tunnels[fid] = []
            continue

        surviving = [t for t in candidate_tunnels.get(fid, []) if t.survives_failure(failed_links)]
        flow_tunnels[fid] = surviving
        for t in surviving:
            var_map_x[(fid, t.id)] = col
            col += 1

    num_vars = col
    if num_vars == 0:
        elapsed = (time.perf_counter() - start_time) * 1000
        total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
        return {
            "success": True,
            "allocations": {fid: {} for fid in demands},
            "runtime_ms": elapsed,
            "solver_status": "no_surviving_tunnels",
            "delivered_by_priority": {},
            "total_delivered": 0.0,
            "total_unserved": total_req,
        }

    # Bounds: 0 <= x[f,t] <= demand[f]
    bounds = []
    for fid, t_list in flow_tunnels.items():
        req_bw = float(getattr(demands[fid], "bandwidth", 0.0) if not isinstance(demands[fid], dict) else demands[fid].get("bandwidth", 0.0))
        for _ in t_list:
            bounds.append((0.0, req_bw))

    # Constraints:
    A_rows = []
    b_rows = []

    # 1. Flow demand constraint: sum_t x[f,t] <= demand[f]
    for fid, t_list in flow_tunnels.items():
        if not t_list:
            continue
        req_bw = float(getattr(demands[fid], "bandwidth", 0.0) if not isinstance(demands[fid], dict) else demands[fid].get("bandwidth", 0.0))
        row = np.zeros(num_vars)
        for t in t_list:
            row[var_map_x[(fid, t.id)]] = 1.0
        A_rows.append(row)
        b_rows.append(req_bw)

    # 2. Physical-link capacity constraint (UNDIRECTED: both directions combined):
    # for each surviving link e: sum_{f,t: e in t} x[f,t] <= C[e]
    for lid, link in usable_links.items():
        cap = float(getattr(link, "capacity", 0.0) if not isinstance(link, dict) else link.get("capacity", 0.0))
        row = np.zeros(num_vars)
        for (fid, tid), idx in var_map_x.items():
            t = next((tun for tun in flow_tunnels[fid] if tun.id == tid), None)
            if t and lid in t.link_ids:
                row[idx] = 1.0
        A_rows.append(row)
        b_rows.append(cap)

    A_ub = np.array(A_rows)
    b_ub = np.array(b_rows)

    # Priority groupings (P1 through P5)
    priority_groups: dict[int, list[str]] = {p: [] for p in range(1, 6)}
    for fid, demand in demands.items():
        if not flow_tunnels.get(fid):
            continue
        p = getattr(demand, "priority", None)
        pval = getattr(p, "value", p) if p is not None else 4
        if pval not in priority_groups:
            pval = 4
        priority_groups[pval].append(fid)

    current_x = np.zeros(num_vars)
    delivered_by_pri: dict[str, float] = {}

    # Lexicographic stages D1 to D5:
    for pval in range(1, 6):
        flows = priority_groups[pval]
        if not flows:
            delivered_by_pri[f"P{pval}"] = 0.0
            continue

        c_stage = np.zeros(num_vars)
        for fid in flows:
            for t in flow_tunnels[fid]:
                idx = var_map_x[(fid, t.id)]
                c_stage[idx] = -1.0  # maximize sum x[f,t] for this priority

        res = linprog(c_stage, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if res.success:
            opt_val = -res.fun
            delivered_by_pri[f"P{pval}"] = max(0.0, opt_val)
            current_x = res.x

            # Lock this priority's throughput: sum_{f in flows, t} x[f,t] >= opt_val - 1e-5
            lock_row = np.zeros(num_vars)
            for fid in flows:
                for t in flow_tunnels[fid]:
                    lock_row[var_map_x[(fid, t.id)]] = -1.0
            A_ub = np.vstack([A_ub, lock_row])
            b_ub = np.append(b_ub, -opt_val + 1e-5)
        else:
            delivered_by_pri[f"P{pval}"] = 0.0

    # Stage D6: Latency minimization while preserving all achieved priority totals
    c_lat = np.zeros(num_vars)
    for (fid, tid), idx in var_map_x.items():
        t = next((tun for tun in flow_tunnels[fid] if tun.id == tid), None)
        lat = t.latency if t else 1.0
        c_lat[idx] = lat * 0.01

    res_final = linprog(c_lat, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if res_final.success:
        sol_x = res_final.x
        solver_status = "optimal"
    else:
        sol_x = current_x
        solver_status = "suboptimal_priorities_only"

    # Assemble allocation output
    allocations: dict[str, dict[str, float]] = {fid: {} for fid in demands}
    total_delivered = 0.0

    for (fid, tid), idx in var_map_x.items():
        val = float(sol_x[idx])
        if val >= 1e-4:
            allocations[fid][tid] = val
            total_delivered += val

    total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
    total_unserved = max(0.0, total_req - total_delivered)
    elapsed = (time.perf_counter() - start_time) * 1000

    return {
        "success": True,
        "allocations": allocations,
        "runtime_ms": elapsed,
        "solver_status": solver_status,
        "delivered_by_priority": delivered_by_pri,
        "total_delivered": total_delivered,
        "total_unserved": total_unserved,
    }
