"""
Bandwidth Allocation Module
============================
Handles applying routing results back to the simulation state,
link utilization updates, and preemption logic.
"""

from __future__ import annotations
from typing import Optional
import networkx as nx

from app.models import (
    Demand, DemandStatus, Priority, LinkStatus, Link,
)
from app.state import SimulationState
from app.routing.baseline import build_graph


def apply_routing_results(state: SimulationState, results: list[Demand]):
    """Apply routing results to the simulation state."""
    # Reset all link loads
    for link_id, link in state.links.items():
        state.links[link_id] = link.model_copy(update={
            "allocated_load": 0.0,
            "utilization": 0.0,
        })

    # Update demands and accumulate link loads
    G = build_graph(state)
    link_loads: dict[str, float] = {lid: 0.0 for lid in state.links}

    for result in results:
        state.demands[result.id] = result
        if result.route and result.allocated_bandwidth > 0:
            for i in range(len(result.route) - 1):
                u, v = result.route[i], result.route[i + 1]
                if G.has_edge(u, v):
                    link_id = G.edges[u, v]["link_id"]
                    link_loads[link_id] = link_loads.get(link_id, 0.0) + result.allocated_bandwidth

    # Update link utilizations
    for link_id, load in link_loads.items():
        link = state.links.get(link_id)
        if link and state.is_link_usable(link_id):
            util = (load / link.capacity * 100) if link.capacity > 0 else 0.0
            state.links[link_id] = link.model_copy(update={
                "allocated_load": load,
                "utilization": util,
            })

    state.bump_version()


def get_congestion_warnings(state: SimulationState, threshold: float = 80.0) -> list[dict]:
    """Identify links approaching high utilization."""
    warnings = []
    for link in state.links.values():
        if state.is_link_usable(link.id) and link.utilization >= threshold:
            # Find which demands use this link
            using_demands = []
            G = build_graph(state)
            for d in state.demands.values():
                if d.route and d.allocated_bandwidth > 0:
                    for i in range(len(d.route) - 1):
                        u, v = d.route[i], d.route[i + 1]
                        if G.has_edge(u, v):
                            edge_link_id = G.edges[u, v]["link_id"]
                            if edge_link_id == link.id:
                                using_demands.append(d.id)
                                break

            warnings.append({
                "link_id": link.id,
                "source": link.source,
                "target": link.target,
                "utilization": link.utilization,
                "capacity": link.capacity,
                "load": link.allocated_load,
                "contributing_demands": using_demands,
                "explanation": (
                    f"Link {link.source}↔{link.target} at {link.utilization:.1f}% utilization "
                    f"({link.allocated_load:.1f}/{link.capacity:.1f} Mbps). "
                    f"Contributing flows: {', '.join(using_demands)}."
                ),
            })
    return warnings


def validate_allocation(state: SimulationState) -> list[str]:
    """Validate all allocation invariants. Returns list of violations."""
    violations = []
    
    for d in state.demands.values():
        # Allocated must be non-negative
        if d.allocated_bandwidth < -0.001:
            violations.append(f"Demand {d.id}: negative allocated bandwidth {d.allocated_bandwidth}")
        
        # Allocated must not exceed requested
        if d.allocated_bandwidth > d.bandwidth + 0.001:
            violations.append(
                f"Demand {d.id}: allocated {d.allocated_bandwidth} > requested {d.bandwidth}"
            )
        
        # Conservation: allocated + unserved = requested
        total = d.allocated_bandwidth + d.unserved_bandwidth
        if abs(total - d.bandwidth) > 0.01:
            violations.append(
                f"Demand {d.id}: allocated({d.allocated_bandwidth}) + "
                f"unserved({d.unserved_bandwidth}) = {total} ≠ requested({d.bandwidth})"
            )

        # Route validity
        if d.route and d.allocated_bandwidth > 0:
            for i in range(len(d.route) - 1):
                u, v = d.route[i], d.route[i + 1]
                # Check nodes exist
                if u not in state.nodes:
                    violations.append(f"Demand {d.id}: route node {u} not in topology")
                if v not in state.nodes:
                    violations.append(f"Demand {d.id}: route node {v} not in topology")
                # Check edge exists and is usable
                found = False
                for link in state.links.values():
                    if ((link.source == u and link.target == v) or
                        (link.source == v and link.target == u)):
                        if not state.is_link_usable(link.id):
                            violations.append(
                                f"Demand {d.id}: route uses failed link {link.id} ({u}↔{v})"
                            )
                        found = True
                        break
                if not found:
                    violations.append(
                        f"Demand {d.id}: route edge {u}↔{v} not found in topology"
                    )

    # Check link capacity violations
    for link in state.links.values():
        if state.is_link_usable(link.id):
            if link.allocated_load > link.capacity + 0.01:
                violations.append(
                    f"Link {link.id}: load {link.allocated_load} exceeds "
                    f"capacity {link.capacity}"
                )
        else:
            if link.allocated_load > 0.001:
                violations.append(
                    f"Link {link.id}: failed link has non-zero load {link.allocated_load}"
                )

    return violations
