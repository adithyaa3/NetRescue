"""
Metrics Computation
====================
Calculates all simulation metrics from current state.
"""

from __future__ import annotations
import math
from app.state import SimulationState
from app.models import (
    DemandStatus, Priority, PRIORITY_LABELS, LinkStatus,
    LinkMetrics, DemandMetrics,
)
from app.routing.baseline import build_graph


def delivered_bandwidth(state: SimulationState, demand) -> float:
    """Configured bandwidth on an unavailable path is not delivered bandwidth."""
    if demand.source == demand.destination:
        return min(demand.bandwidth, demand.allocated_bandwidth)
    if demand.id in state.active_allocations:
        lookup = {t.id: t for t in state.candidate_tunnels.get(demand.id, [])}
        total = 0.0
        for tid, rate in state.active_allocations[demand.id].items():
            tunnel = lookup.get(tid)
            if tunnel and all(state.is_link_usable(lid) for lid in tunnel.link_ids):
                total += rate
        return min(demand.bandwidth, total)
    if not demand.route:
        return 0.0
    for u, v in zip(demand.route, demand.route[1:]):
        if not any({l.source, l.target} == {u, v} and state.is_link_usable(lid)
                   for lid, l in state.links.items()):
            return 0.0
    return min(demand.bandwidth, demand.allocated_bandwidth)


def integrate_critical_loss(intervals) -> float:
    """Integrate (per-flow demand - delivered)+ over supplied durations in seconds.

    Each interval is (duration_seconds, critical_demand_by_id, delivered_by_id).
    The synchronous controller does not itself generate elapsed-time intervals.
    """
    loss = 0.0
    for duration, demands, delivered in intervals:
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("Interval duration must be finite and nonnegative")
        for fid, demand in demands.items():
            rate = delivered.get(fid, 0.0)
            if not math.isfinite(demand) or not math.isfinite(rate) or demand < 0 or rate < 0:
                raise ValueError("Demand and delivery must be finite and nonnegative")
            loss += max(0.0, demand-rate)*duration
    return loss


def compute_metrics(state: SimulationState) -> dict:
    """Compute comprehensive simulation metrics."""
    total_requested = 0.0
    total_delivered = 0.0
    total_unserved = 0.0
    critical_requested = 0.0
    critical_delivered = 0.0
    general_requested = 0.0
    general_delivered = 0.0
    disconnected = 0
    active_demands = 0
    
    per_priority: dict[str, dict] = {}
    demand_details = []
    
    total_latency = 0.0
    latency_count = 0
    
    G = build_graph(state)

    for d in state.demands.values():
        delivered = delivered_bandwidth(state, d)
        unserved = max(0.0, d.bandwidth-delivered)
        total_requested += d.bandwidth
        total_delivered += delivered
        total_unserved += unserved
        
        is_critical = d.priority.value in (Priority.P1_EMERGENCY.value, Priority.P2_ESSENTIAL.value)
        if is_critical:
            critical_requested += d.bandwidth
            critical_delivered += delivered
        else:
            general_requested += d.bandwidth
            general_delivered += delivered
        
        if d.status == DemandStatus.UNREACHABLE:
            disconnected += 1
        if d.status in (DemandStatus.FULLY_SERVED, DemandStatus.PARTIALLY_SERVED):
            active_demands += 1
            
        # Compute route latency
        if d.route and len(d.route) >= 2 and d.allocated_bandwidth > 0:
            route_lat = 0.0
            for i in range(len(d.route) - 1):
                u, v = d.route[i], d.route[i + 1]
                if G.has_edge(u, v):
                    route_lat += G.edges[u, v].get("latency", 0)
            total_latency += route_lat
            latency_count += 1

        # Per-priority stats
        pkey = f"P{d.priority.value}"
        if pkey not in per_priority:
            per_priority[pkey] = {
                "label": PRIORITY_LABELS.get(d.priority, pkey),
                "requested": 0.0,
                "delivered": 0.0,
                "unserved": 0.0,
                "count": 0,
            }
        per_priority[pkey]["requested"] += d.bandwidth
        per_priority[pkey]["delivered"] += delivered
        per_priority[pkey]["unserved"] += unserved
        per_priority[pkey]["count"] += 1
        
        demand_details.append(DemandMetrics(
            id=d.id,
            source=d.source,
            destination=d.destination,
            category=d.category.value,
            priority=d.priority.value,
            requested=d.bandwidth,
            allocated=delivered,
            unserved=unserved,
            status=d.status.value,
            route=d.route,
        ))

    # Link metrics
    link_utils = []
    max_util = 0.0
    total_util = 0.0
    util_count = 0
    failed_links = 0

    for l in state.links.values():
        if not state.is_link_usable(l.id):
            failed_links += 1
        else:
            max_util = max(max_util, l.utilization)
            total_util += l.utilization
            util_count += 1

        link_utils.append(LinkMetrics(
            id=l.id,
            capacity=l.capacity,
            allocated_load=l.allocated_load,
            utilization=l.utilization,
            status=l.status.value,
        ))

    avg_util = total_util / util_count if util_count > 0 else 0.0
    avg_latency = total_latency / latency_count if latency_count > 0 else 0.0
    
    critical_delivery_pct = (
        (critical_delivered / critical_requested * 100)
        if critical_requested > 0 else 100.0
    )
    general_delivery_pct = (
        (general_delivered / general_requested * 100)
        if general_requested > 0 else 100.0
    )

    return {
        "version": state.version,
        "total_requested": round(total_requested, 2),
        "total_delivered": round(total_delivered, 2),
        "total_unserved": round(total_unserved, 2),
        "critical_delivery_pct": round(critical_delivery_pct, 2),
        "general_delivery_pct": round(general_delivery_pct, 2),
        "critical_requested": round(critical_requested, 2),
        "critical_delivered": round(critical_delivered, 2),
        "general_requested": round(general_requested, 2),
        "general_delivered": round(general_delivered, 2),
        "failed_links": failed_links,
        "disconnected_demands": disconnected,
        "active_demands": active_demands,
        "max_link_utilization": round(max_util, 2),
        "avg_link_utilization": round(avg_util, 2),
        "avg_latency": round(avg_latency, 2),
        "link_utilizations": [lu.model_dump() for lu in link_utils],
        "demand_details": [dd.model_dump() for dd in demand_details],
        "per_priority": per_priority,
        "route_changes": state.route_changes,
    }
