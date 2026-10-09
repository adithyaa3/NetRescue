"""
What-If Analysis
=================
Runs hypothetical failure scenarios on a snapshot without modifying live state.
"""

from __future__ import annotations
from app.state import SimulationState
from app.models import Priority
from app.routing.pacar import run_pacar
from app.routing.allocation import apply_routing_results, get_congestion_warnings
from app.analysis.metrics import compute_metrics


def run_what_if(state: SimulationState, link_ids: list[str]) -> dict:
    """
    Simulate hypothetical link failures.
    
    1. Snapshot current state.
    2. Compute current metrics (before).
    3. Fail specified links in the snapshot.
    4. Re-route and compute new metrics (after).
    5. Return comparison without modifying live state.
    """
    # Before: current state metrics
    before_snap = state.snapshot()
    before_results = run_pacar(before_snap)
    apply_routing_results(before_snap, before_results)
    before_metrics = compute_metrics(before_snap)
    
    # After: fail links and re-route
    after_snap = state.snapshot()
    actually_failed = []
    for lid in link_ids:
        if lid in after_snap.links:
            after_snap.fail_link(lid)
            actually_failed.append(lid)
    
    after_results = run_pacar(after_snap)
    apply_routing_results(after_snap, after_results)
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
        f"What-if analysis: failing {len(actually_failed)} link(s) "
        f"[{', '.join(actually_failed)}]."
    )
    
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
        "failed_links": actually_failed,
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
