"""
Algorithm G: Protection Guarantee Checker
=========================================
Independently verifies proactive protection plans across every declared scenario.
Clones network assumptions, applies failures, simulates Reflex recovery,
validates through Algorithm F (Validator), and asserts that every critical flow
receives at least its declared guaranteed Mbps.
"""

from __future__ import annotations
import math
import copy
from dataclasses import dataclass, field
from typing import Any

from app.routing.candidate_paths import CandidateTunnel
from app.routing.protection_planner import ProtectionPlan
from app.routing.reflex import run_reflex_recovery
from app.routing.validator import validate_allocation_independently, value


@dataclass
class GuaranteeVerificationResult:
    is_verified: bool
    scenarios_tested: int
    scenarios_passed: int
    failures: list[str] = field(default_factory=list)


def verify_protection_guarantees(
    plan: ProtectionPlan,
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    tolerance: float = 1e-4,
) -> GuaranteeVerificationResult:
    """
    Independently verifies that the candidate ProtectionPlan delivers its stated
    guaranteed Mbps for each critical flow across EVERY covered scenario.
    Never mutates live simulation state.
    """
    result = GuaranteeVerificationResult(
        is_verified=True,
        scenarios_tested=len(plan.covered_scenarios),
        scenarios_passed=0,
    )

    # Flatten candidate tunnels for validator lookup
    tunnel_lookup: dict[str, CandidateTunnel] = {}
    for fid, t_list in candidate_tunnels.items():
        for t in t_list:
            tunnel_lookup[t.id] = t

    # Validate the plan itself before trying scaled recovery allocations.
    if plan.solver_status.startswith(("infeasible", "failed")):
        result.failures.append("Protection solver did not produce a feasible plan")
    if not plan.covered_scenarios:
        result.failures.append("Protection plan declares no covered scenarios")
    for scenario in plan.covered_scenarios:
        lid = scenario[5:] if scenario.startswith("fail_") else scenario
        if scenario != "no_failure" and lid not in links:
            result.failures.append(f"Unknown covered scenario: {scenario}")
    for fid, guarantee in plan.per_flow_guaranteed.items():
        if fid not in demands or not math.isfinite(guarantee) or guarantee < 0:
            result.failures.append(f"Invalid guarantee for flow {fid}")
        elif guarantee > value(demands[fid], "bandwidth", 0) + tolerance:
            result.failures.append(f"Guarantee exceeds demand for flow {fid}")
    reservation_demands = copy.deepcopy(demands)
    for fid, reservations in plan.per_tunnel_reservations.items():
        if fid not in demands:
            result.failures.append(f"Unknown reserved flow {fid}")
            continue
        if any(not isinstance(a, (int, float)) or not math.isfinite(a) or a < 0 for a in reservations.values()):
            result.failures.append(f"Invalid reservations for flow {fid}")
            continue
        # Total reservation can exceed demand because backups reserve in parallel.
        demand = reservation_demands[fid]
        total = sum(reservations.values())
        if isinstance(demand, dict):
            demand["bandwidth"] = max(total, value(demand, "bandwidth", 0))
        else:
            reservation_demands[fid] = demand.model_copy(update={"bandwidth": max(total, demand.bandwidth)})
    reservation_check = validate_allocation_independently(
        plan.per_tunnel_reservations, reservation_demands, links, tunnel_lookup)
    result.failures.extend(reservation_check.violations)
    if result.failures:
        result.is_verified = False
        plan.verified = False
        plan.verification_failures = result.failures
        return result

    for scenario in plan.covered_scenarios:
        # Determine failed links in this scenario
        failed_links: set[str] = set()
        if scenario.startswith("fail_"):
            lid = scenario[len("fail_"):]
            failed_links.add(lid)
        elif scenario != "no_failure":
            failed_links.add(scenario)

        # 1. Run Reflex in candidate verification mode
        reflex_out = run_reflex_recovery(
            failed_links=failed_links,
            protection_plan=plan,
            demands=demands,
            links=links,
            candidate_tunnels=candidate_tunnels,
            previous_allocations=None,
            is_verification_mode=True,
        )

        if not reflex_out["success"]:
            result.is_verified = False
            result.failures.append(f"Scenario '{scenario}': Reflex failed: {reflex_out.get('message')}")
            continue

        allocations = reflex_out["allocations"]

        # 2. Run independent validator
        val = validate_allocation_independently(
            allocations=allocations,
            demands=demands,
            links=links,
            tunnels=tunnel_lookup,
            failed_links=failed_links,
        )

        if not val.is_valid:
            result.is_verified = False
            for v in val.violations:
                result.failures.append(f"Scenario '{scenario}' validation violation: {v}")
            continue

        # 3. Check guaranteed bandwidth for each critical flow
        scenario_ok = True
        for fid, guaranteed_bw in plan.per_flow_guaranteed.items():
            if guaranteed_bw <= tolerance:
                continue

            delivered = sum(allocations.get(fid, {}).values())
            if delivered < guaranteed_bw - tolerance:
                result.is_verified = False
                scenario_ok = False
                result.failures.append(
                    f"Scenario '{scenario}': Flow '{fid}' delivered {delivered:.2f} Mbps, "
                    f"failed to meet claimed guarantee {guaranteed_bw:.2f} Mbps"
                )

        if scenario_ok:
            result.scenarios_passed += 1

    plan.verified = result.is_verified
    plan.verification_failures = result.failures
    return result
