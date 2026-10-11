"""
Baseline Routing — Capacity-Constrained Dijkstra
=================================================
Processes demands sorted by priority (then by ID for determinism).
Uses Dijkstra shortest-path based on latency.
Respects link capacities. Supports partial allocation.
"""

from __future__ import annotations
import networkx as nx
from typing import Optional

from app.models import (
    Demand, DemandStatus, Priority, LinkStatus,
)
from app.state import SimulationState


def build_graph(state: SimulationState) -> nx.Graph:
    """Build a NetworkX graph from active/usable links."""
    G = nx.Graph()
    for n in state.nodes.values():
        G.add_node(n.id)
    for l in state.links.values():
        if state.is_link_usable(l.id):
            G.add_edge(
                l.source, l.target,
                link_id=l.id,
                capacity=l.capacity,
                latency=l.latency,
                weight=l.latency,  # Dijkstra weight = latency
            )
    return G


def get_residual_capacity(state: SimulationState, link_id: str) -> float:
    """Get remaining capacity on a link."""
    link = state.links.get(link_id)
    if link is None or not state.is_link_usable(link_id):
        return 0.0
    return max(0.0, link.capacity - link.allocated_load)


def path_bottleneck(state: SimulationState, path: list[str], G: nx.Graph, residual: dict[str, float]) -> float:
    """Get the minimum residual capacity along a path."""
    if len(path) < 2:
        return 0.0
    min_cap = float("inf")
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        edge_data = G.edges[u, v]
        link_id = edge_data["link_id"]
        cap = residual.get(link_id, 0.0)
        min_cap = min(min_cap, cap)
    return min_cap if min_cap != float("inf") else 0.0


def path_latency(path: list[str], G: nx.Graph) -> float:
    """Get total latency of a path."""
    if len(path) < 2:
        return 0.0
    total = 0.0
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        total += G.edges[u, v]["latency"]
    return total


def allocate_on_path(
    state: SimulationState,
    path: list[str],
    bandwidth: float,
    G: nx.Graph,
    residual: dict[str, float],
):
    """Allocate bandwidth along a path, updating residual capacities."""
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        link_id = G.edges[u, v]["link_id"]
        residual[link_id] -= bandwidth


def _trace_order(sorted_demands) -> list[dict]:
    return [{"id": d.id, "priority": d.priority.value, "source": d.source,
             "destination": d.destination, "bandwidth": d.bandwidth,
             "max_latency": d.max_latency} for d in sorted_demands]


def run_baseline(state: SimulationState, trace=None) -> list[Demand]:
    """
    Capacity-constrained Dijkstra baseline.
    
    Demand processing order: sorted by priority (ascending = higher first),
    then by demand ID for deterministic tie-breaking.
    
    For each demand:
    1. Find shortest path by latency (Dijkstra).
    2. Check bottleneck capacity.
    3. Allocate full bandwidth if possible, partial if not.
    4. If no path exists, mark unreachable.

    ``trace`` (optional TraceRecorder) receives a passive record of every step;
    it is never read, so it cannot change the result.
    """
    G = build_graph(state)
    
    # Initialize residual capacities
    residual: dict[str, float] = {}
    for l in state.links.values():
        if state.is_link_usable(l.id):
            residual[l.id] = l.capacity
        else:
            residual[l.id] = 0.0

    # Sort demands: by priority (lower number = higher priority), then by ID
    sorted_demands = sorted(
        state.demands.values(),
        key=lambda d: (d.priority.value, d.id)
    )

    results = []
    if trace is not None:
        trace.emit("baseline.init", residual=residual,
                   capacity={l.id: l.capacity for l in state.links.values()},
                   unusable=[l.id for l in state.links.values() if not state.is_link_usable(l.id)],
                   order=_trace_order(sorted_demands))

    for demand in sorted_demands:
        # Handle local demands
        if demand.source == demand.destination:
            results.append(demand.model_copy(update={
                "status": DemandStatus.LOCAL,
                "allocated_bandwidth": demand.bandwidth,
                "unserved_bandwidth": 0.0,
                "route": [demand.source],
                "explanation": "Local traffic: source equals destination.",
            }))
            if trace is not None:
                trace.emit("baseline.local", demand=demand.id)
            continue

        # Check connectivity
        if demand.source not in G or demand.destination not in G:
            results.append(demand.model_copy(update={
                "status": DemandStatus.UNREACHABLE,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "explanation": f"Node not in active network.",
            }))
            if trace is not None:
                trace.emit("baseline.unreachable", demand=demand.id, reason="node_not_in_active_network")
            continue

        if not nx.has_path(G, demand.source, demand.destination):
            results.append(demand.model_copy(update={
                "status": DemandStatus.UNREACHABLE,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "explanation": f"No path exists between {demand.source} and {demand.destination}.",
            }))
            if trace is not None:
                trace.emit("baseline.unreachable", demand=demand.id, reason="no_path")
            continue

        # Find shortest path by latency
        try:
            # Build a subgraph with only links that have residual capacity > 0
            feasible_G = nx.Graph()
            for u, v, data in G.edges(data=True):
                link_id = data["link_id"]
                if residual.get(link_id, 0.0) > 0:
                    feasible_G.add_edge(u, v, **data)

            if (demand.source not in feasible_G or
                demand.destination not in feasible_G or
                not nx.has_path(feasible_G, demand.source, demand.destination)):
                # Try original graph for partial allocation
                path = nx.shortest_path(G, demand.source, demand.destination, weight="weight")
                used_feasible_graph = False
            else:
                path = nx.shortest_path(feasible_G, demand.source, demand.destination, weight="weight")
                used_feasible_graph = True
        except nx.NetworkXNoPath:
            results.append(demand.model_copy(update={
                "status": DemandStatus.UNREACHABLE,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "explanation": f"No path exists between {demand.source} and {demand.destination}.",
            }))
            if trace is not None:
                trace.emit("baseline.unreachable", demand=demand.id, reason="no_path")
            continue

        bottleneck = path_bottleneck(state, path, G, residual)
        latency = path_latency(path, G)
        if trace is not None:
            path_links = [G.edges[u, v]["link_id"] for u, v in zip(path, path[1:])]
            trace.emit("baseline.path", demand=demand.id, path=path, links=path_links,
                       hop_residual=[[lid, residual.get(lid, 0.0)] for lid in path_links],
                       used_feasible_graph=used_feasible_graph,
                       saturated=sorted(lid for lid, r in residual.items()
                                        if state.is_link_usable(lid) and r <= 0),
                       latency=latency, bottleneck=bottleneck,
                       bandwidth=demand.bandwidth, max_latency=demand.max_latency)
        if demand.max_latency is not None and demand.max_latency > 0 and latency > demand.max_latency:
            results.append(demand.model_copy(update={
                "status": DemandStatus.UNSERVED_CONGESTION,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "explanation": "No residual-capacity path satisfies the latency limit.",
            }))
            if trace is not None:
                trace.emit("baseline.decision", demand=demand.id, outcome="latency_limit",
                           allocated=0.0, residual=residual)
            continue

        if bottleneck >= demand.bandwidth:
            # Full allocation
            allocate_on_path(state, path, demand.bandwidth, G, residual)
            results.append(demand.model_copy(update={
                "status": DemandStatus.FULLY_SERVED,
                "allocated_bandwidth": demand.bandwidth,
                "unserved_bandwidth": 0.0,
                "route": path,
                "explanation": (
                    f"Shortest path {' → '.join(path)} "
                    f"(latency {latency:.1f}ms) with sufficient capacity."
                ),
            }))
            if trace is not None:
                trace.emit("baseline.decision", demand=demand.id, outcome="full",
                           allocated=demand.bandwidth, residual=residual)
        elif bottleneck > 0:
            # Partial allocation
            allocate_on_path(state, path, bottleneck, G, residual)
            results.append(demand.model_copy(update={
                "status": DemandStatus.PARTIALLY_SERVED,
                "allocated_bandwidth": bottleneck,
                "unserved_bandwidth": demand.bandwidth - bottleneck,
                "route": path,
                "explanation": (
                    f"Path {' → '.join(path)} has bottleneck of {bottleneck:.1f} Mbps "
                    f"(requested {demand.bandwidth:.1f} Mbps). Partially served."
                ),
            }))
            if trace is not None:
                trace.emit("baseline.decision", demand=demand.id, outcome="partial",
                           allocated=bottleneck, residual=residual)
        else:
            # No capacity
            results.append(demand.model_copy(update={
                "status": DemandStatus.UNSERVED_CONGESTION,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "explanation": (
                    f"Path exists but no residual capacity on route "
                    f"{' → '.join(path)}. All links are fully utilized."
                ),
            }))
            if trace is not None:
                trace.emit("baseline.decision", demand=demand.id, outcome="no_capacity",
                           allocated=0.0, residual=residual)

    if trace is not None:
        trace.emit("baseline.done", results=[
            {"id": r.id, "status": r.status.value, "allocated": r.allocated_bandwidth,
             "requested": r.bandwidth, "route": r.route} for r in results])
    return results
