"""
What-If Analysis
=================
Runs hypothetical failure scenarios on a snapshot without modifying live state.

The hypothetical outcome is produced by the same controller pipeline that a real
failure would trigger, executed on a private copy of the network:
- one newly failed link: Reflex (if the verified plan covers it) or PACAR fallback,
  then the Reactive LP under the install policy;
- several links: the batch handler (PACAR re-simulation).
So "after" is what the controller would actually deliver, not a separate estimate.
"""

from __future__ import annotations
from app.state import SimulationState
from app.routing.allocation import get_congestion_warnings
from app.analysis.metrics import compute_metrics


def run_what_if(state: SimulationState, link_ids: list[str]) -> dict:
    """
    Simulate hypothetical link failures.

    1. Snapshot current state.
    2. Compute current metrics (before) from the actual installed allocation.
    3. Replay the controller's failure handling on a second snapshot.
    4. Return the comparison without modifying live state.
    """
    from app.simulation.engine import handle_link_failure, handle_batch_failures

    if not link_ids:
        raise ValueError("Select at least one link")
    unknown = sorted(set(link_ids) - set(state.links))
    if unknown:
        raise ValueError(f"Unknown link IDs: {', '.join(unknown)}")
    link_ids = list(dict.fromkeys(link_ids))
    # Before: actual current allocation, not a hypothetical PACAR recomputation.
    before_snap = state.snapshot()
    before_metrics = compute_metrics(before_snap)

    # After: the controller's real response, on a private copy.
    after_snap = state.snapshot()
    already_unavailable = [lid for lid in link_ids if not after_snap.is_link_usable(lid)]
    to_fail = [lid for lid in link_ids if lid not in already_unavailable]
    controller = {"mode": "none", "reflex_used": False}
    if len(to_fail) == 1:
        out = handle_link_failure(after_snap, to_fail[0])
        controller = {"mode": "single_failure", "reflex_used": bool(out.get("reflex_used"))}
    elif to_fail:
        handle_batch_failures(after_snap, to_fail)
        controller = {"mode": "batch_failure", "reflex_used": False}
    after_metrics = compute_metrics(after_snap)

    # Find unreachable demands
    unreachable = [
        d.id for d in after_snap.demands.values()
        if d.status.value == "unreachable"
    ]

    # Find congested demands
    congested = [
        d.id for d in after_snap.demands.values()
        if d.status.value in ("partially_served", "unserved_congestion")
    ]

    # Alternative routes
    alt_routes = {}
    for d in after_snap.demands.values():
        orig = state.demands.get(d.id)
        if orig and orig.route != d.route:
            alt_routes[d.id] = {
                "old_route": orig.route,
                "new_route": d.route,
                "old_bandwidth": orig.allocated_bandwidth,
                "new_bandwidth": d.allocated_bandwidth,
            }

    # High risk links (>80% util after failure)
    warnings = get_congestion_warnings(after_snap, threshold=80.0)
    high_risk = [w["link_id"] for w in warnings]

    explanation_parts = []
    explanation_parts.append(
        f"What-if analysis: failing {len(link_ids)} link(s) [{', '.join(link_ids)}]."
    )
    if already_unavailable:
        explanation_parts.append(
            f"Already unavailable: {', '.join(already_unavailable)}."
        )
    if controller["mode"] == "single_failure":
        explanation_parts.append(
            "Recovery: Reflex (verified plan) then Reactive LP." if controller["reflex_used"]
            else "Recovery: PACAR fallback then Reactive LP (not covered by a verified plan)."
        )
    elif controller["mode"] == "batch_failure":
        explanation_parts.append("Recovery: multi-link failure handled by PACAR re-simulation.")

    total_lost = before_metrics["total_delivered"] - after_metrics["total_delivered"]
    crit_lost = before_metrics["critical_delivered"] - after_metrics["critical_delivered"]

    if total_lost > 0:
        explanation_parts.append(
            f"Total throughput drops by {total_lost:.1f} Mbps."
        )
    if crit_lost > 0:
        explanation_parts.append(
            f"Critical traffic drops by {crit_lost:.1f} Mbps."
        )
    if unreachable:
        explanation_parts.append(
            f"{len(unreachable)} demand(s) become unreachable."
        )
    if high_risk:
        explanation_parts.append(
            f"{len(high_risk)} link(s) at risk of congestion."
        )

    return {
        "failed_links": link_ids,
        "already_unavailable": already_unavailable,
        "recovery_mode": controller["mode"],
        "reflex_used": controller["reflex_used"],
        "total_delivered_before": round(before_metrics["total_delivered"], 2),
        "total_delivered_after": round(after_metrics["total_delivered"], 2),
        "critical_delivered_before": round(before_metrics["critical_delivered"], 2),
        "critical_delivered_after": round(after_metrics["critical_delivered"], 2),
        "general_delivered_before": round(before_metrics["general_delivered"], 2),
        "general_delivered_after": round(after_metrics["general_delivered"], 2),
        "unreachable_demands": unreachable,
        "congested_demands": congested,
        "alternative_routes": alt_routes,
        "high_risk_links": high_risk,
        "congestion_warnings": [w for w in warnings],
        "explanation": " ".join(explanation_parts),
        "before_metrics": before_metrics,
        "after_metrics": after_metrics,
    }
