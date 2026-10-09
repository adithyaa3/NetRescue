"""
Algorithm G: Protection Guarantee Checker
=========================================
Independently verifies proactive protection plans across every declared scenario.
Clones network assumptions, applies failures, simulates Reflex recovery,
validates through Algorithm F (Validator), and asserts that every critical flow
receives at least its declared guaranteed Mbps.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from app.routing.candidate_paths import CandidateTunnel
from app.routing.protection_planner import ProtectionPlan
from app.routing.reflex import run_reflex_recovery
from app.routing.validator import validate_allocation_independently


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
