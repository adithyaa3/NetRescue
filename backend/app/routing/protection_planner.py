"""
Algorithm B: Proactive Protection Planner (HiGHS LP)
===================================================
Solves a lexicographic linear program to compute verified bandwidth guarantees
and tunnel reservations for P1 (Emergency) and P2 (Essential Infrastructure) flows
under single physical-link failure scenarios (plus no-failure).

Shared capacity constraint: sum of reservations on physical link e in either
direction <= C[e].
Individual guarantee locking: Locks each flow's b[f] before reservation minimization.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional, Any
import numpy as np
from scipy.optimize import linprog
from types import SimpleNamespace

from app.routing.validator import tunnel_errors, value
from app.models import Priority
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


@dataclass
class ProtectionPlan:
    plan_id: str
    topology_version: int
    demand_version: int
    covered_scenarios: list[str]  # e.g. ["no_failure", "fail_L1", "fail_L2", ...]
    per_flow_guaranteed: dict[str, float]  # flow_id -> guaranteed_mbps
    per_tunnel_reservations: dict[str, dict[str, float]]  # flow_id -> {tunnel_id -> reserved_mbps}
    solver_status: str
    runtime_ms: float
    verified: bool = False
    verification_failures: list[str] = field(default_factory=list)
    is_active: bool = True
    # Links that were already unavailable (failed or held down) when the plan was
    # computed. The plan protects the network as it was then: its baseline is
    # "these links down", and each covered scenario fails one more usable link.
    base_unavailable: list[str] = field(default_factory=list)

    def get_flow_guarantee(self, flow_id: str) -> float:
        return self.per_flow_guaranteed.get(flow_id, 0.0)

    def scenario_for(self, unavailable_links) -> Optional[str]:
        """Covered scenario matching a set of unavailable links, or None if uncovered.

        Links in the plan baseline that have since been restored are harmless:
        reservations never use them, so only additional failures matter.
        """
        extra = set(unavailable_links) - set(self.base_unavailable)
        if not extra:
            return "no_failure" if "no_failure" in self.covered_scenarios else None
        if len(extra) == 1:
            lid = next(iter(extra))
            if f"fail_{lid}" in self.covered_scenarios:
                return f"fail_{lid}"
            if lid in self.covered_scenarios:
                return lid
        return None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "topology_version": self.topology_version,
            "demand_version": self.demand_version,
            "covered_scenarios_count": len(self.covered_scenarios),
            "per_flow_guaranteed": {k: round(v, 2) for k, v in self.per_flow_guaranteed.items()},
            "solver_status": self.solver_status,
            "runtime_ms": round(self.runtime_ms, 2),
            "verified": self.verified,
            "verification_failures": self.verification_failures,
        }


def plan_proactive_protection(
    critical_demands: dict[str, Any],  # flow_id -> Demand (P1, P2 only)
    links: dict[str, Any],            # link_id -> Link
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    topology_version: int = 1,
    demand_version: int = 1,
    trace=None,
) -> ProtectionPlan:
    """
    Computes proactive protection guarantees for critical flows using SciPy HiGHS LP.
    ``trace`` (optional TraceRecorder) receives a passive record of the model and of
    each lexicographic stage; it is never read, so it cannot change the plan.
    """
    start_time = time.perf_counter()
    plan_id = f"plan_v{topology_version}_{demand_version}_{int(time.time())}"

    # Filter critical flows (Priority 1 and 2)
    filtered_flows: dict[str, Any] = {}
    for fid, d in critical_demands.items():
        p = value(d, "priority")
        if hasattr(p, "value"):
            pval = p.value
        elif isinstance(p, int):
            pval = p
        else:
            pval = 4
        if pval in (Priority.P1_EMERGENCY.value, Priority.P2_ESSENTIAL.value, 1, 2):
            filtered_flows[fid] = d

    if trace is not None:
        def _pv(d):
            p = value(d, "priority")
            return getattr(p, "value", p) if p is not None else 4
        trace.emit("plan.inputs",
                   critical=[{"id": fid, "priority": _pv(d), "bandwidth": float(value(d, "bandwidth", 0.0)),
                              "source": value(d, "source"), "destination": value(d, "destination")}
                             for fid, d in filtered_flows.items()],
                   not_planned=[{"id": fid, "priority": _pv(d)} for fid, d in critical_demands.items()
                                if fid not in filtered_flows])

    if not filtered_flows:
        if trace is not None:
            trace.emit("plan.trivial", reason="no_critical_flows")
        elapsed = (time.perf_counter() - start_time) * 1000
        return ProtectionPlan(
            plan_id=plan_id,
            topology_version=topology_version,
            demand_version=demand_version,
            covered_scenarios=["no_failure"],
            per_flow_guaranteed={fid: 0.0 for fid in critical_demands},
            per_tunnel_reservations={fid: {} for fid in critical_demands},
            solver_status="trivial_empty",
            runtime_ms=elapsed,
            verified=False,
        )

    # Plan for the network as it is now: links that are already failed or held down
    # cannot carry reservations, and failing them again is not a new scenario.
    unavailable = sorted(lid for lid, link in links.items()
                         if str(getattr(value(link, "status"), "value", value(link, "status"))) in ("failed", "flapping"))
    usable_links = {lid: link for lid, link in links.items() if lid not in unavailable}

    # Scenarios: "no_failure" (current network) + fail(lid) for each usable link lid
    scenarios = ["no_failure"] + [f"fail_{lid}" for lid in usable_links.keys()]
    if trace is not None:
        trace.emit("plan.scenarios", unavailable=unavailable, usable=list(usable_links.keys()),
                   scenarios=scenarios)

    # Index variables
    # For each critical flow f and tunnel t: a[f,t] >= 0
    # For each critical flow f: b[f] >= 0
    var_map_a: dict[tuple[str, str], int] = {}  # (flow_id, tunnel_id) -> var_idx
    var_map_b: dict[str, int] = {}              # flow_id -> var_idx

    col = 0
    flow_tunnels: dict[str, list[CandidateTunnel]] = {}
    for fid, d in filtered_flows.items():
        t_list = [t for t in candidate_tunnels.get(fid, [])
                  if not tunnel_errors(t, fid, d, links, failed_links=set(unavailable))]
        flow_tunnels[fid] = t_list
        for t in t_list:
            var_map_a[(fid, t.id)] = col
            col += 1
        var_map_b[fid] = col
        col += 1

    num_vars = col
    if trace is not None:
        variables_rec = {}
        for fid, d in filtered_flows.items():
            kept = {t.id for t in flow_tunnels[fid]}
            variables_rec[fid] = {
                "tunnels": [t.to_dict() for t in flow_tunnels[fid]],
                "excluded": [{"tunnel": t.to_dict(),
                              "reasons": tunnel_errors(t, fid, d, links, failed_links=set(unavailable))}
                             for t in candidate_tunnels.get(fid, []) if t.id not in kept],
            }
        trace.emit("plan.variables", flows=variables_rec, num_vars=num_vars)
    if num_vars == 0:
        elapsed = (time.perf_counter() - start_time) * 1000
        if trace is not None:
            trace.emit("plan.no_tunnels")
        return ProtectionPlan(
            plan_id=plan_id,
            topology_version=topology_version,
            demand_version=demand_version,
            covered_scenarios=scenarios,
            per_flow_guaranteed={fid: 0.0 for fid in filtered_flows},
            per_tunnel_reservations={fid: {} for fid in filtered_flows},
            solver_status="no_tunnels",
            runtime_ms=elapsed,
            verified=False,
            base_unavailable=unavailable,
        )

    # Variable bounds:
    # a[f,t] >= 0
    # 0 <= b[f] <= demand[f]
    bounds = []
    for fid, d in filtered_flows.items():
        t_list = flow_tunnels[fid]
        bw = getattr(d, "bandwidth", 0.0)
        if isinstance(d, dict):
            bw = d.get("bandwidth", 0.0)
        for _ in t_list:
            bounds.append((0.0, None))
        bounds.append((0.0, float(bw)))

    # Constraints:
    # 1. Capacity: for each link e: sum_{f,t: e in t} a[f,t] <= C[e]
    A_rows = []
    b_rows = []

    for lid, link in usable_links.items():
        cap = getattr(link, "capacity", 0.0)
        if isinstance(link, dict):
            cap = link.get("capacity", 0.0)
        row = np.zeros(num_vars)
        for (fid, tid), idx in var_map_a.items():
            t = next((tun for tun in flow_tunnels[fid] if tun.id == tid), None)
            if t and lid in t.link_ids:
                row[idx] = 1.0
        A_rows.append(row)
        b_rows.append(float(cap))

    if trace is not None:
        trace.emit("plan.capacity_rows", rows=[
            {"link": lid, "capacity": float(value(link, "capacity", 0.0)),
             "variables": [[fid, tid] for (fid, tid) in var_map_a
                           if lid in next(t for t in flow_tunnels[fid] if t.id == tid).link_ids]}
            for lid, link in usable_links.items()])

    # 2. Survival: for each critical flow f and scenario q:
    # sum_{t surviving q} a[f,t] >= b[f]
    # => b[f] - sum_{t surviving q} a[f,t] <= 0
    survival_rec = {} if trace is not None else None
    for fid in filtered_flows:
        b_idx = var_map_b[fid]
        t_list = flow_tunnels[fid]
        for sc in scenarios:
            row = np.zeros(num_vars)
            row[b_idx] = 1.0  # + b[f]
            failed_in_sc = set()
            if sc.startswith("fail_"):
                failed_in_sc.add(sc[len("fail_"):])
            if survival_rec is not None:
                survival_rec.setdefault(fid, {})[sc] = []

            for t in t_list:
                if t.survives_failure(failed_in_sc):
                    a_idx = var_map_a[(fid, t.id)]
                    row[a_idx] = -1.0
                    if survival_rec is not None:
                        survival_rec[fid][sc].append(t.id)
            A_rows.append(row)
            b_rows.append(0.0)
    if trace is not None:
        trace.emit("plan.survival", matrix=survival_rec, scenarios=scenarios)

    A_ub = np.array(A_rows)
    b_ub = np.array(b_rows)

    # ── Stage B1: Maximize P1 guaranteed bandwidth ────────────────────
    c_stage1 = np.zeros(num_vars)
    for fid, d in filtered_flows.items():
        p = value(d, "priority")
        pval = getattr(p, "value", p) if p is not None else 4
        if pval in (Priority.P1_EMERGENCY.value, 1):
            c_stage1[var_map_b[fid]] = -1.0  # minimize -b[f] = maximize b[f]

    res1 = _solve_lp(c_stage1, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if trace is not None:
        trace.emit("plan.stage", stage="B1", objective="maximize the total guarantee of P1 flows",
                   flows=[fid for fid in filtered_flows if c_stage1[var_map_b[fid]] != 0],
                   optimum=(-res1.fun) if res1.success else None, solver=_solver_info(res1))
    if not res1.success:
        elapsed = (time.perf_counter() - start_time) * 1000
        return ProtectionPlan(
            plan_id=plan_id,
            topology_version=topology_version,
            demand_version=demand_version,
            covered_scenarios=scenarios,
            per_flow_guaranteed={fid: 0.0 for fid in filtered_flows},
            per_tunnel_reservations={fid: {} for fid in filtered_flows},
            solver_status=f"infeasible: {res1.message}",
            runtime_ms=elapsed,
            verified=False,
            base_unavailable=unavailable,
        )

    opt_p1 = -res1.fun

    # Lock P1 optimum: sum_{f in P1} b[f] >= opt_p1
    # => -sum_{f in P1} b[f] <= -opt_p1
    p1_lock_row = np.zeros(num_vars)
    for fid, d in filtered_flows.items():
        p = value(d, "priority")
        pval = getattr(p, "value", p) if p is not None else 4
        if pval in (Priority.P1_EMERGENCY.value, 1):
            p1_lock_row[var_map_b[fid]] = -1.0

    A_ub = np.vstack([A_ub, p1_lock_row])
    b_ub = np.append(b_ub, -opt_p1)

    # ── Stage B2: Maximize P2 guaranteed bandwidth ────────────────────
    c_stage2 = np.zeros(num_vars)
    for fid, d in filtered_flows.items():
        p = value(d, "priority")
        pval = getattr(p, "value", p) if p is not None else 4
        if pval in (Priority.P2_ESSENTIAL.value, 2):
            c_stage2[var_map_b[fid]] = -1.0

    res2 = _solve_lp(c_stage2, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if trace is not None:
        trace.emit("plan.stage", stage="B2",
                   objective="maximize the total guarantee of P2 flows while keeping the P1 optimum",
                   flows=[fid for fid in filtered_flows if c_stage2[var_map_b[fid]] != 0],
                   optimum=(-res2.fun) if res2.success else None, solver=_solver_info(res2),
                   p1_locked=opt_p1)
    if res2.success:
        opt_p2 = -res2.fun
        p2_lock_row = np.zeros(num_vars)
        for fid, d in filtered_flows.items():
            p = value(d, "priority")
            pval = getattr(p, "value", p) if p is not None else 4
            if pval in (Priority.P2_ESSENTIAL.value, 2):
                p2_lock_row[var_map_b[fid]] = -1.0
        A_ub = np.vstack([A_ub, p2_lock_row])
        b_ub = np.append(b_ub, -opt_p2)
        current_x = res2.x
    else:
        current_x = res1.x

    # Extract final b[f] per flow
    final_b: dict[str, float] = {}
    for fid in filtered_flows:
        val = current_x[var_map_b[fid]]
        final_b[fid] = max(0.0, float(val))
    if trace is not None:
        trace.emit("plan.guarantees_chosen", guarantees=final_b, from_stage="B2" if res2.success else "B1")

    # ── Stage B4: CRITICAL REQUIREMENT ────────────────────────────────
    # Lock EACH individual b[f] to its chosen value before minimizing reservations:
    # b[f] >= final_b[f]  =>  -b[f] <= -final_b[f]
    # b[f] <= final_b[f]
    lock_rows = []
    lock_rhs = []
    for fid, val in final_b.items():
        idx = var_map_b[fid]
        r1 = np.zeros(num_vars)
        r1[idx] = -1.0
        lock_rows.append(r1)
        lock_rhs.append(-val)

        r2 = np.zeros(num_vars)
        r2[idx] = 1.0
        lock_rows.append(r2)
        lock_rhs.append(val)

    A_ub = np.vstack([A_ub, np.array(lock_rows)])
    b_ub = np.append(b_ub, np.array(lock_rhs))

    # Minimize total reservations: sum a[f,t]
    c_stage4 = np.zeros(num_vars)
    for (fid, tid), idx in var_map_a.items():
        c_stage4[idx] = 1.0  # minimize sum a[f,t]

    res4 = _solve_lp(c_stage4, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if res4.success:
        sol_x = res4.x
        solver_status = "optimal" if res2.success else "suboptimal_stage1"
    else:
        sol_x = current_x
        solver_status = "suboptimal_stage2"
    if trace is not None:
        trace.emit("plan.stage", stage="B4",
                   objective="minimize total reserved bandwidth with every guarantee locked",
                   flows=list(filtered_flows),
                   optimum=float(res4.fun) if res4.success else None, solver=_solver_info(res4),
                   locked=final_b)

    # Assemble per-tunnel reservations and per-flow guarantees
    per_flow_guaranteed: dict[str, float] = {}
    per_tunnel_reservations: dict[str, dict[str, float]] = {fid: {} for fid in critical_demands}

    for fid in filtered_flows:
        g = max(0.0, float(sol_x[var_map_b[fid]]))
        # Round very small numerical artifacts to 0
        if g < 1e-9:
            g = 0.0
        per_flow_guaranteed[fid] = g

        for t in flow_tunnels[fid]:
            a_idx = var_map_a[(fid, t.id)]
            a_val = max(0.0, float(sol_x[a_idx]))
            if a_val >= 1e-9:
                per_tunnel_reservations[fid][t.id] = a_val

    # Non-critical flows get 0 guarantee
    for fid in critical_demands:
        if fid not in per_flow_guaranteed:
            per_flow_guaranteed[fid] = 0.0

    elapsed = (time.perf_counter() - start_time) * 1000
    if trace is not None:
        trace.emit("plan.result", guarantees=per_flow_guaranteed, reservations=per_tunnel_reservations,
                   solver_status=solver_status, scenarios=scenarios, base_unavailable=unavailable,
                   tunnels={f"{fid}|{t.id}": t.to_dict() for fid, ts in flow_tunnels.items() for t in ts})

    return ProtectionPlan(
        plan_id=plan_id,
        topology_version=topology_version,
        demand_version=demand_version,
        covered_scenarios=scenarios,
        per_flow_guaranteed=per_flow_guaranteed,
        per_tunnel_reservations=per_tunnel_reservations,
        solver_status=solver_status,
        runtime_ms=elapsed,
        verified=False,  # Must be verified by Guarantee Checker
        base_unavailable=unavailable,
    )
