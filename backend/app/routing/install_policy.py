"""
Algorithm H: Allocation Install Policy
======================================
Evaluates candidate allocations against active allocations and decides whether
to install.
Rules:
1. Reject invalid allocations (checked via independent validator).
2. Reject stale state versions.
3. Reject allocations that use failed physical links.
4. If current allocation is invalidated by failure, always install a valid recovery
   allocation even if total delivered bandwidth decreases.
5. Preserve lexicographic priority (higher priority cannot be sacrificed for lower).
6. Meaningful improvement threshold applied for normal re-optimizations to prevent churn.
7. Logs all install decisions with structured explanations.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any

from app.models import Priority, LinkStatus
from app.routing.candidate_paths import CandidateTunnel
from app.routing.validator import validate_allocation_independently, ValidationResult


@dataclass
class InstallDecision:
    should_install: bool
    reason: str
    decision_type: str  # "installed_recovery", "installed_improvement", "rejected_invalid", "rejected_stale", "rejected_churn"
    candidate_delivered: float
    current_delivered: float
    violations: list[str] = field(default_factory=list)


def evaluate_install_policy(
    candidate_allocation: dict[str, dict[str, float]],
    current_allocation: dict[str, dict[str, float]],
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[CandidateTunnel]],
    expected_version: int,
    current_version: int,
    trigger_reason: str = "normal",  # "link_failure", "link_restore", "demand_change", "normal"
    churn_threshold_ratio: float = 0.01,  # 1% threshold to avoid unnecessary churn
) -> InstallDecision:
    """
    Evaluates whether candidate_allocation should be installed into the active state.
    """
    # Flatten candidate tunnels for validator lookup
    tunnel_lookup: dict[str, CandidateTunnel] = {}
    for fid, t_list in candidate_tunnels.items():
        for t in t_list:
            tunnel_lookup[t.id] = t

    failed_links = set()
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status == "failed":
            failed_links.add(lid)

    # 1. Reject stale versions (unless recovery from failure)
    if expected_version < current_version and trigger_reason != "link_failure":
        return InstallDecision(
            should_install=False,
            reason=f"Stale state version: candidate computed for v{expected_version}, current is v{current_version}",
            decision_type="rejected_stale",
            candidate_delivered=0.0,
            current_delivered=0.0,
        )

    # 2. Independent validation check
    val = validate_allocation_independently(
        allocations=candidate_allocation,
        demands=demands,
        links=links,
        tunnels=tunnel_lookup,
        failed_links=failed_links,
    )

    if not val.is_valid:
        return InstallDecision(
            should_install=False,
            reason=f"Candidate allocation failed validation: {'; '.join(val.violations[:3])}",
            decision_type="rejected_invalid",
            candidate_delivered=val.total_delivered,
            current_delivered=0.0,
            violations=val.violations,
        )

    cand_delivered = val.total_delivered

    # 3. Check if current allocation is valid under current state
    curr_val = validate_allocation_independently(
        allocations=current_allocation,
        demands=demands,
        links=links,
        tunnels=tunnel_lookup,
        failed_links=failed_links,
    )

    # If current allocation is invalid (e.g. because a link just failed),
    # ALWAYS install the valid candidate recovery allocation!
    if not curr_val.is_valid or trigger_reason == "link_failure":
        return InstallDecision(
            should_install=True,
            reason=f"Installed recovery allocation (current allocation invalid under current failures). Delivered: {cand_delivered:.1f} Mbps",
            decision_type="installed_recovery",
            candidate_delivered=cand_delivered,
            current_delivered=curr_val.total_delivered if curr_val.is_valid else 0.0,
        )

    curr_delivered = curr_val.total_delivered

    # 4. Compare priority deliveries (Lexicographic comparison)
    # Never sacrifice higher-priority delivered bandwidth for lower-priority!
    def compute_priority_sums(alloc: dict[str, dict[str, float]]) -> dict[int, float]:
        sums = {p: 0.0 for p in range(1, 6)}
        for fid, t_alloc in alloc.items():
            d = demands.get(fid)
            if not d:
                continue
            p = getattr(d, "priority", None)
            pval = getattr(p, "value", p) if p is not None else 4
            if pval not in sums:
                pval = 4
            sums[pval] += sum(t_alloc.values())
        return sums

    cand_pri = compute_priority_sums(candidate_allocation)
    curr_pri = compute_priority_sums(current_allocation)

    # Check P1 to P5 lexicographically
    for p in range(1, 6):
        if cand_pri[p] < curr_pri[p] - 1e-4:
            return InstallDecision(
                should_install=False,
                reason=f"Candidate reduces P{p} traffic ({cand_pri[p]:.1f} < {curr_pri[p]:.1f} Mbps); rejected by priority policy.",
                decision_type="rejected_priority",
                candidate_delivered=cand_delivered,
                current_delivered=curr_delivered,
            )
        elif cand_pri[p] > curr_pri[p] + 1e-4:
            # Candidate strictly improves higher priority! Install immediately.
            return InstallDecision(
                should_install=True,
                reason=f"Candidate improves P{p} delivered bandwidth ({cand_pri[p]:.1f} > {curr_pri[p]:.1f} Mbps).",
                decision_type="installed_improvement",
                candidate_delivered=cand_delivered,
                current_delivered=curr_delivered,
            )

    # If priority delivery is identical, check total throughput with churn threshold
    total_demand = sum(float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0)) for d in demands.values())
    threshold = total_demand * churn_threshold_ratio

    if cand_delivered > curr_delivered + threshold:
        return InstallDecision(
            should_install=True,
            reason=f"Candidate delivers more total bandwidth ({cand_delivered:.1f} > {curr_delivered:.1f} Mbps).",
            decision_type="installed_improvement",
            candidate_delivered=cand_delivered,
            current_delivered=curr_delivered,
        )

    # Identical or minor difference: avoid unnecessary route churn
    return InstallDecision(
        should_install=False,
        reason=f"Candidate does not significantly improve delivery ({cand_delivered:.1f} vs {curr_delivered:.1f} Mbps); avoided route churn.",
        decision_type="rejected_churn",
        candidate_delivered=cand_delivered,
        current_delivered=curr_delivered,
    )
