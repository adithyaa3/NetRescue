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
from types import SimpleNamespace

from app.routing.validator import tunnel_errors, value
from app.models import Priority, LinkStatus
from app.routing.candidate_paths import CandidateTunnel


def _solve_lp(*args, **kwargs):
    try:
        return linprog(*args, **kwargs)
    except (RuntimeError, ValueError) as exc:
        return SimpleNamespace(success=False, message=str(exc))


def _solver_info(res) -> dict:
    """Solver outcome fields reported by SciPy/HiGHS (for the Algorithm Explorer)."""
    return {"success": bool(getattr(res, "success", False)),
            "status": getattr(res, "status", None),
            "message": str(getattr(res, "message", "")),
            "iterations": getattr(res, "nit", None)}


def _excluded_tunnels(fid, demand, req_bw, candidate_tunnels, surviving, links, failed_links) -> list[dict]:
    kept = {t.id for t in surviving}
    excluded = []
    for t in candidate_tunnels.get(fid, []):
        if t.id in kept:
            continue
        if req_bw <= 0:
            reasons = ["flow has no positive demand"]
        elif not t.survives_failure(failed_links):
            reasons = [f"crosses unavailable link {lid}" for lid in t.link_ids if lid in failed_links]
        else:
            reasons = tunnel_errors(t, fid, demand, links, failed_links)
        excluded.append({"tunnel": t.to_dict(), "reasons": reasons})
    return excluded


def solve_reactive_multicommodity_lp(
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    previous_allocations: Optional[dict[str, dict[str, float]]] = None,
    beta_churn: float = 0.05,
    protection_floors: Optional[dict[str, float]] = None,
    trace=None,
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
    ``previous_allocations`` and ``beta_churn`` are accepted for interface
    compatibility; the objective does not use them.
    ``trace`` (optional TraceRecorder) receives a passive record of the model and of
    each lexicographic stage (objective, optimum, solver status); it is never read.
    """
    start_time = time.perf_counter()

    # Identify surviving links
    failed_links = set()
    usable_links = {}
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status in ("failed", "flapping"):
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

        surviving = [t for t in candidate_tunnels.get(fid, []) if t.survives_failure(failed_links) and not tunnel_errors(t, fid, demand, links, failed_links)]
        flow_tunnels[fid] = surviving
        for t in surviving:
            var_map_x[(fid, t.id)] = col
            col += 1

    num_vars = col
    if trace is not None:
        flows_rec = []
        for fid, demand in demands.items():
            req_bw = float(getattr(demand, "bandwidth", 0.0) if not isinstance(demand, dict) else demand.get("bandwidth", 0.0))
            p = value(demand, "priority")
            flows_rec.append({
                "id": fid,
                "priority": getattr(p, "value", p) if p is not None else 4,
                "source": value(demand, "source"), "destination": value(demand, "destination"),
                "bandwidth": req_bw,
                "variables": [t.to_dict() for t in flow_tunnels.get(fid, [])],
                "excluded": _excluded_tunnels(fid, demand, req_bw, candidate_tunnels,
                                              flow_tunnels.get(fid, []), links, failed_links),
            })
        trace.emit("lp.inputs", failed_links=sorted(failed_links), flows=flows_rec,
                   num_vars=num_vars, floors=dict(protection_floors or {}))
    if num_vars == 0:
        elapsed = (time.perf_counter() - start_time) * 1000
        total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
        if trace is not None:
            trace.emit("lp.no_variables",
                       success=not any(floor > 0 for floor in (protection_floors or {}).values()),
                       floors=dict(protection_floors or {}))
        return {
            "success": not any(floor > 0 for floor in (protection_floors or {}).values()),
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

    for fid, floor in (protection_floors or {}).items():
        if floor <= 0:
            continue
        row = np.zeros(num_vars)
        for t in flow_tunnels.get(fid, []):
            row[var_map_x[(fid, t.id)]] = -1.0
        A_rows.append(row)
        b_rows.append(-float(floor))

    A_ub = np.array(A_rows)
    b_ub = np.array(b_rows)
    if trace is not None:
        trace.emit("lp.model",
                   demand_rows=[{"flow": fid, "bound": float(getattr(demands[fid], "bandwidth", 0.0) if not isinstance(demands[fid], dict) else demands[fid].get("bandwidth", 0.0)),
                                 "variables": [t.id for t in t_list]}
                                for fid, t_list in flow_tunnels.items() if t_list],
                   capacity_rows=[{"link": lid,
                                   "capacity": float(getattr(link, "capacity", 0.0) if not isinstance(link, dict) else link.get("capacity", 0.0)),
                                   "variables": [tid for (fid, tid) in var_map_x
                                                 if lid in next(t for t in flow_tunnels[fid] if t.id == tid).link_ids]}
                                  for lid, link in usable_links.items()],
                   floor_rows=[{"flow": fid, "floor": float(floor)}
                               for fid, floor in (protection_floors or {}).items() if floor > 0],
                   num_vars=num_vars, num_rows=int(A_ub.shape[0]))

    # Priority groupings (P1 through P5)
    priority_groups: dict[int, list[str]] = {p: [] for p in range(1, 6)}
    for fid, demand in demands.items():
        if not flow_tunnels.get(fid):
            continue
        p = value(demand, "priority")
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
            if trace is not None:
                trace.emit("lp.stage", priority=pval, flows=[], skipped=True)
            continue

        c_stage = np.zeros(num_vars)
        for fid in flows:
            for t in flow_tunnels[fid]:
                idx = var_map_x[(fid, t.id)]
                c_stage[idx] = -1.0  # maximize sum x[f,t] for this priority

        res = _solve_lp(c_stage, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if res.success:
            opt_val = -res.fun
            delivered_by_pri[f"P{pval}"] = max(0.0, opt_val)
            current_x = res.x

            # Lock this priority's throughput: sum_{f in flows, t} x[f,t] >= opt_val
            lock_row = np.zeros(num_vars)
            for fid in flows:
                for t in flow_tunnels[fid]:
                    lock_row[var_map_x[(fid, t.id)]] = -1.0
            A_ub = np.vstack([A_ub, lock_row])
            b_ub = np.append(b_ub, -opt_val)
            if trace is not None:
                trace.emit("lp.stage", priority=pval, flows=list(flows), skipped=False,
                           optimum=opt_val, solver=_solver_info(res),
                           requested=sum(float(value(demands[f], "bandwidth", 0.0)) for f in flows))
        else:
            if trace is not None:
                trace.emit("lp.stage", priority=pval, flows=list(flows), skipped=False,
                           optimum=None, solver=_solver_info(res),
                           requested=sum(float(value(demands[f], "bandwidth", 0.0)) for f in flows))
            return {"success": False, "allocations": {},
                    "runtime_ms": (time.perf_counter()-start_time)*1000,
                    "solver_status": f"failed_P{pval}: {res.message}",
                    "delivered_by_priority": {}, "total_delivered": 0.0,
                    "total_unserved": sum(value(d, "bandwidth", 0) for d in demands.values())}

    # Stage D6: Latency minimization while preserving all achieved priority totals
    c_lat = np.zeros(num_vars)
    for (fid, tid), idx in var_map_x.items():
        t = next((tun for tun in flow_tunnels[fid] if tun.id == tid), None)
        lat = t.latency if t else 1.0
        c_lat[idx] = lat * 0.01

    res_final = _solve_lp(c_lat, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if res_final.success:
        sol_x = res_final.x
        solver_status = "optimal"
    else:
        sol_x = current_x
        solver_status = "suboptimal_priorities_only"
    if trace is not None:
        trace.emit("lp.latency_stage", solver=_solver_info(res_final),
                   objective=getattr(res_final, "fun", None) if res_final.success else None,
                   weights={f"{fid}|{tid}": float(c_lat[idx]) for (fid, tid), idx in var_map_x.items()},
                   solver_status=solver_status)

    # Assemble allocation output
    allocations: dict[str, dict[str, float]] = {fid: {} for fid in demands}
    total_delivered = 0.0

    for (fid, tid), idx in var_map_x.items():
        val = float(sol_x[idx])
        if val >= 1e-9:
            allocations[fid][tid] = val
            total_delivered += val

    total_req = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
    total_unserved = max(0.0, total_req - total_delivered)
    elapsed = (time.perf_counter() - start_time) * 1000
    if trace is not None:
        trace.emit("lp.result", allocations=allocations, delivered_by_priority=delivered_by_pri,
                   total_delivered=total_delivered, total_unserved=total_unserved,
                   solver_status=solver_status,
                   tunnels={f"{fid}|{t.id}": t.to_dict() for fid, ts in flow_tunnels.items() for t in ts})

    return {
        "success": True,
        "allocations": allocations,
        "runtime_ms": elapsed,
        "solver_status": solver_status,
        "delivered_by_priority": delivered_by_pri,
        "total_delivered": total_delivered,
        "total_unserved": total_unserved,
    }
