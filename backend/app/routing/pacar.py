"""
PACAR — Priority-Aware Capacity-Adaptive Routing
=================================================
Project-specific heuristic combining established graph algorithms
with priority-aware bandwidth allocation.

PACAR is NOT an established standardized routing protocol. It is our
hackathon-designed combination of:
- Dijkstra shortest-path
- Yen's K-shortest simple paths
- Residual capacity tracking
- Priority-aware bandwidth allocation
- Congestion-aware path scoring
- Capacity-feasible fallback search
- Emergency preemption

Path Scoring Formula
--------------------
score(path) = w_latency * (latency / max_latency)
            + w_congestion * (max_utilization_on_path)
            + w_hop * (hop_count / max_hops)
            - w_capacity * (bottleneck_capacity / max_capacity)

Lower score = better path.

Default weights:
  w_latency    = 0.30  (prefer lower latency)
  w_congestion = 0.35  (prefer less congested paths)
  w_hop        = 0.10  (slight preference for fewer hops)
  w_capacity   = 0.25  (prefer higher available capacity)

Trade-offs:
- Congestion weight is highest to spread load across the network.
- Capacity weight rewards paths with headroom.
- Latency is secondary to congestion avoidance.
- Hop count has low weight; longer paths are acceptable if less congested.

Equal-Priority Fairness
-----------------------
When multiple demands share the same priority and compete for bottleneck
capacity, PACAR uses progressive filling:
  1. Group equal-priority demands competing for the same bottleneck.
  2. Divide available capacity equally among competing demands.
  3. Each demand gets min(equal_share, requested_bandwidth).
  4. Remaining capacity is redistributed to unsatisfied demands.

This is an approximation of max-min fairness at the bottleneck level,
not a globally optimal max-min fair allocation. For a hackathon simulator,
this provides defensible and testable fairness behavior.
"""

from __future__ import annotations
import networkx as nx
from itertools import islice
from app.routing.validator import tunnel_errors, value
from typing import Optional

from app.models import (
    Demand, DemandStatus, Priority, LinkStatus, TrafficCategory,
)
from app.state import SimulationState
from app.routing.baseline import (
    build_graph, path_bottleneck, path_latency, allocate_on_path,
)


# ── Scoring weights ──────────────────────────────────────────────────
W_LATENCY = 0.30
W_CONGESTION = 0.35
W_HOP = 0.10
W_CAPACITY = 0.25

K_PATHS = 5  # Number of candidate paths from Yen's algorithm


def path_score_breakdown(
    path: list[str],
    G: nx.Graph,
    residual: dict[str, float],
    state: SimulationState,
    max_latency: float = 100.0,
    max_hops: int = 10,
    max_capacity: float = 1000.0,
) -> dict:
    """
    Score a candidate path and return the score together with its terms.
    ``compute_path_score`` returns ``["score"]`` of this dictionary; the
    Algorithm Explorer displays the individual terms.
    """
    if len(path) < 2:
        return {"score": float("inf")}

    total_latency = path_latency(path, G)
    hop_count = len(path) - 1
    
    max_util = 0.0
    min_residual = float("inf")
    
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        if not G.has_edge(u, v):
            return {"score": float("inf")}
        edge_data = G.edges[u, v]
        link_id = edge_data["link_id"]
        link = state.links.get(link_id)
        if link is None:
            return {"score": float("inf")}
        
        res = residual.get(link_id, 0.0)
        cap = link.capacity
        util = 1.0 - (res / cap) if cap > 0 else 1.0
        max_util = max(max_util, util)
        min_residual = min(min_residual, res)

    if min_residual == float("inf"):
        min_residual = 0.0

    # Normalize
    norm_latency = min(total_latency / max(max_latency, 1.0), 1.0)
    norm_hops = min(hop_count / max(max_hops, 1), 1.0)
    norm_capacity = min(min_residual / max(max_capacity, 1.0), 1.0)

    score = (
        W_LATENCY * norm_latency
        + W_CONGESTION * max_util
        + W_HOP * norm_hops
        - W_CAPACITY * norm_capacity
    )
    return {
        "score": score,
        "latency": total_latency,
        "hops": hop_count,
        "max_util": max_util,
        "min_residual": min_residual,
        "norm_latency": norm_latency,
        "norm_hops": norm_hops,
        "norm_capacity": norm_capacity,
    }


def compute_path_score(
    path: list[str],
    G: nx.Graph,
    residual: dict[str, float],
    state: SimulationState,
    max_latency: float = 100.0,
    max_hops: int = 10,
    max_capacity: float = 1000.0,
) -> float:
    """
    Score a candidate path. Lower = better.
    
    Components:
    - Normalized latency (0-1)
    - Max utilization on any link in the path (0-1)
    - Normalized hop count (0-1)
    - Normalized bottleneck capacity (0-1), subtracted (higher capacity = lower score)
    """
    return path_score_breakdown(path, G, residual, state, max_latency=max_latency,
                                max_hops=max_hops, max_capacity=max_capacity)["score"]


def _path_links(G: nx.Graph, path: list[str]) -> list[str]:
    return [G.edges[u, v]["link_id"] for u, v in zip(path, path[1:]) if G.has_edge(u, v)]


def find_k_shortest_paths(
    G: nx.Graph,
    source: str,
    target: str,
    k: int = K_PATHS,
) -> list[list[str]]:
    """Find K shortest simple paths using NetworkX."""
    try:
        paths = list(islice(nx.shortest_simple_paths(G, source, target, weight="weight"), max(0, k)))
        return paths[:k]
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def find_capacity_feasible_path(
    G: nx.Graph,
    source: str,
    target: str,
    required_bw: float,
    residual: dict[str, float],
    state: SimulationState,
    max_paths: int = 20,
    max_latency: Optional[float] = None,
    trace=None,
) -> Optional[list[str]]:
    """
    Fallback: search beyond K paths for one with enough capacity.
    Uses simple paths generator with capacity check.
    """
    examined = [] if trace is not None else None
    limit_reached = False
    try:
        for i, path in enumerate(
            nx.shortest_simple_paths(G, source, target, weight="weight")
        ):
            if i >= max_paths:
                limit_reached = True
                break
            if max_latency is not None and max_latency > 0 and path_latency(path, G) > max_latency:
                if examined is not None:
                    examined.append({"path": path, "latency": path_latency(path, G),
                                     "verdict": "exceeds_latency_limit"})
                continue
            bottleneck = path_bottleneck(state, path, G, residual)
            if bottleneck >= required_bw:
                if examined is not None:
                    examined.append({"path": path, "latency": path_latency(path, G),
                                     "bottleneck": bottleneck, "verdict": "accepted"})
                    trace.emit("pacar.fallback_search", source=source, target=target,
                               required=required_bw, max_paths=max_paths, examined=examined,
                               limit_reached=False, found=path)
                return path
            if examined is not None:
                examined.append({"path": path, "latency": path_latency(path, G),
                                 "bottleneck": bottleneck, "verdict": "insufficient_capacity"})
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    if trace is not None:
        trace.emit("pacar.fallback_search", source=source, target=target, required=required_bw,
                   max_paths=max_paths, examined=examined, limit_reached=limit_reached, found=None)
    return None


def run_pacar(state: SimulationState, trace=None) -> list[Demand]:
    """
    PACAR routing algorithm.
    
    Steps:
    1. Build graph from active/usable links.
    2. Initialize residual capacities.
    3. Sort demands by priority (ascending = higher first), then by ID.
    4. For each priority group:
       a. Identify competing demands (same bottleneck links).
       b. Apply progressive filling for equal-priority fairness.
    5. For each demand:
       a. Find K candidate paths.
       b. Score each path using the scoring formula.
       c. Select best feasible path.
       d. If no K-path is feasible, do capacity-feasible fallback search.
       e. Allocate bandwidth (full or partial).
       f. Generate explanation.
    6. Return all demand results.

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

    # Compute normalization constants
    max_latency = max((l.latency for l in state.links.values()), default=100.0)
    max_latency = max(max_latency, 1.0)
    max_capacity = max((l.capacity for l in state.links.values()), default=1000.0)
    max_hops = max(len(state.nodes), 10)

    # Sort demands by priority then ID
    sorted_demands = sorted(
        state.demands.values(),
        key=lambda d: (d.priority.value, d.id)
    )

    results: list[Demand] = []
    
    # Group demands by priority for fairness handling
    priority_groups: dict[int, list[Demand]] = {}
    for d in sorted_demands:
        pv = d.priority.value
        if pv not in priority_groups:
            priority_groups[pv] = []
        priority_groups[pv].append(d)

    if trace is not None:
        trace.emit("pacar.init", residual=residual,
                   capacity={l.id: l.capacity for l in state.links.values()},
                   unusable=[l.id for l in state.links.values() if not state.is_link_usable(l.id)],
                   weights={"latency": W_LATENCY, "congestion": W_CONGESTION,
                            "hop": W_HOP, "capacity": W_CAPACITY},
                   k_paths=K_PATHS,
                   norm={"max_latency": max_latency, "max_capacity": max_capacity, "max_hops": max_hops},
                   groups={str(p): [d.id for d in g] for p, g in priority_groups.items()},
                   order=[{"id": d.id, "priority": d.priority.value, "source": d.source,
                           "destination": d.destination, "bandwidth": d.bandwidth,
                           "max_latency": d.max_latency} for d in sorted_demands])

    for priority_val in sorted(priority_groups.keys()):
        group = priority_groups[priority_val]
        if trace is not None:
            trace.emit("pacar.group_start", priority=priority_val,
                       demands=[d.id for d in group], residual=residual)
        
        # Phase 1: Find best paths for all demands in this priority group
        demand_paths: dict[str, tuple[list[str], float, float]] = {}  # demand_id -> (path, bottleneck, score)
        
        for demand in group:
            if demand.source == demand.destination:
                results.append(demand.model_copy(update={
                    "status": DemandStatus.LOCAL,
                    "allocated_bandwidth": demand.bandwidth,
                    "unserved_bandwidth": 0.0,
                    "route": [demand.source],
                    "explanation": "Local traffic: source equals destination.",
                }))
                if trace is not None:
                    trace.emit("pacar.local", demand=demand.id)
                continue

            if demand.source not in G or demand.destination not in G:
                results.append(demand.model_copy(update={
                    "status": DemandStatus.UNREACHABLE,
                    "allocated_bandwidth": 0.0,
                    "unserved_bandwidth": demand.bandwidth,
                    "route": None,
                    "explanation": f"Node not in active network.",
                }))
                if trace is not None:
                    trace.emit("pacar.unreachable", demand=demand.id, reason="node_not_in_active_network")
                continue

            if not nx.has_path(G, demand.source, demand.destination):
                results.append(demand.model_copy(update={
                    "status": DemandStatus.UNREACHABLE,
                    "allocated_bandwidth": 0.0,
                    "unserved_bandwidth": demand.bandwidth,
                    "route": None,
                    "explanation": (
                        f"No path exists between {demand.source} and "
                        f"{demand.destination} — network may be partitioned."
                    ),
                }))
                if trace is not None:
                    trace.emit("pacar.unreachable", demand=demand.id, reason="no_path")
                continue

            # Find K candidate paths
            k_paths = find_k_shortest_paths(G, demand.source, demand.destination, K_PATHS)
            candidates = [path for path in k_paths
                          if demand.max_latency is None or demand.max_latency <= 0
                          or path_latency(path, G) <= demand.max_latency]
            
            best_path = None
            best_score = float("inf")
            best_bottleneck = 0.0
            all_candidates_info = []

            for path in candidates:
                bottleneck = path_bottleneck(state, path, G, residual)
                score = compute_path_score(
                    path, G, residual, state,
                    max_latency=max_latency,
                    max_hops=max_hops,
                    max_capacity=max_capacity,
                )
                all_candidates_info.append((path, bottleneck, score))
                
                # Prefer paths that can carry at least some bandwidth
                if bottleneck > 0 and score < best_score:
                    best_score = score
                    best_path = path
                    best_bottleneck = bottleneck

            if trace is not None:
                trace.emit("pacar.candidates", demand=demand.id, priority=priority_val,
                           bandwidth=demand.bandwidth, max_latency=demand.max_latency,
                           candidates=[{"path": path, "links": _path_links(G, path),
                                        "bottleneck": bottleneck, "score": score,
                                        "terms": path_score_breakdown(
                                            path, G, residual, state, max_latency=max_latency,
                                            max_hops=max_hops, max_capacity=max_capacity)}
                                       for path, bottleneck, score in all_candidates_info],
                           dropped_by_latency=[{"path": path, "latency": path_latency(path, G)}
                                               for path in k_paths if path not in candidates],
                           best_path=best_path, best_score=best_score,
                           best_bottleneck=best_bottleneck, residual=residual)

            # If no K-path has capacity, try fallback search
            if best_path is None or best_bottleneck <= 0:
                fallback = find_capacity_feasible_path(
                    G, demand.source, demand.destination,
                    demand.bandwidth, residual, state, max_latency=demand.max_latency,
                    trace=trace,
                )
                fallback_outcome = None
                if fallback:
                    best_path = fallback
                    best_bottleneck = path_bottleneck(state, fallback, G, residual)
                    best_score = compute_path_score(
                        fallback, G, residual, state,
                        max_latency=max_latency,
                        max_hops=max_hops,
                        max_capacity=max_capacity,
                    )
                    fallback_outcome = "capacity_feasible_path"
                elif candidates:
                    # Use best candidate even with partial capacity
                    for path, bottleneck, score in sorted(all_candidates_info, key=lambda x: x[2]):
                        if bottleneck > 0:
                            best_path = path
                            best_bottleneck = bottleneck
                            best_score = score
                            fallback_outcome = "partial_candidate"
                            break
                    if best_path is None and candidates:
                        # All paths have zero capacity
                        best_path = candidates[0]
                        best_bottleneck = 0.0
                        fallback_outcome = "zero_capacity_candidate"
                else:
                    fallback_outcome = "no_candidates"
                if trace is not None:
                    trace.emit("pacar.fallback", demand=demand.id, outcome=fallback_outcome,
                               path=best_path, bottleneck=best_bottleneck, score=best_score,
                               links=_path_links(G, best_path) if best_path else [])

            if best_path is None:
                results.append(demand.model_copy(update={
                    "status": DemandStatus.UNSERVED_CONGESTION,
                    "allocated_bandwidth": 0.0,
                    "unserved_bandwidth": demand.bandwidth,
                    "route": None,
                    "explanation": "No feasible path found after exhaustive search.",
                }))
                if trace is not None:
                    trace.emit("pacar.unserved", demand=demand.id)
                continue

            demand_paths[demand.id] = (best_path, best_bottleneck, best_score)

        # Phase 2: Progressive filling for equal-priority fairness
        pending = {did: demand_paths[did] for did in demand_paths}
        
        if not pending:
            if trace is not None:
                trace.emit("pacar.group_empty", priority=priority_val)
            continue

        demand_by_id = {d.id: d for d in group}
        demand_links: dict[str, list[str]] = {}
        for did, (path, _, _) in pending.items():
            links = []
            for i in range(len(path) - 1):
                u, v = path[i], path[i + 1]
                if G.has_edge(u, v):
                    links.append(G.edges[u, v]["link_id"])
            demand_links[did] = links

        demand_allocation: dict[str, float] = {did: 0.0 for did in pending}
        active_demands = set(pending.keys())
        if trace is not None:
            trace.emit("pacar.fill_start", priority=priority_val,
                       paths={did: {"path": pending[did][0], "links": demand_links[did],
                                    "score": pending[did][2], "bottleneck": pending[did][1]}
                              for did in pending},
                       requested={did: demand_by_id[did].bandwidth for did in pending},
                       residual=residual)
        fill_round = 0

        while active_demands:
            eligible = set()
            for did in active_demands:
                needed = demand_by_id[did].bandwidth - demand_allocation[did]
                if needed <= 1e-9:
                    continue
                links = demand_links[did]
                if not links:
                    continue
                if all(residual.get(lid, 0.0) > 1e-9 for lid in links):
                    eligible.add(did)

            if not eligible:
                if trace is not None:
                    trace.emit("pacar.fill_stop", priority=priority_val, reason="no_eligible_demand",
                               allocation=demand_allocation,
                               needs={did: demand_by_id[did].bandwidth - demand_allocation[did]
                                      for did in pending},
                               saturated=sorted({lid for did in pending for lid in demand_links[did]
                                                 if residual.get(lid, 0.0) <= 1e-9}),
                               residual=residual)
                break

            link_counts: dict[str, int] = {}
            for did in eligible:
                for lid in demand_links[did]:
                    link_counts[lid] = link_counts.get(lid, 0) + 1

            min_delta = float("inf")
            for lid, count in link_counts.items():
                if count > 0:
                    avail = residual.get(lid, 0.0)
                    min_delta = min(min_delta, avail / count)

            for did in eligible:
                needed = demand_by_id[did].bandwidth - demand_allocation[did]
                min_delta = min(min_delta, needed)

            if min_delta <= 1e-9 or min_delta == float("inf"):
                if trace is not None:
                    trace.emit("pacar.fill_stop", priority=priority_val, reason="no_progress",
                               delta=min_delta, allocation=demand_allocation,
                               needs={did: demand_by_id[did].bandwidth - demand_allocation[did]
                                      for did in pending},
                               saturated=[], residual=residual)
                break

            if trace is not None:
                round_shares = {lid: [residual.get(lid, 0.0), count, residual.get(lid, 0.0) / count]
                                for lid, count in link_counts.items() if count > 0}
                round_needs = {did: demand_by_id[did].bandwidth - demand_allocation[did]
                               for did in eligible}
            for did in eligible:
                demand_allocation[did] += min_delta
                for lid in demand_links[did]:
                    residual[lid] = max(0.0, residual.get(lid, 0.0) - min_delta)
            if trace is not None:
                fill_round += 1
                trace.emit("pacar.fill_round", priority=priority_val, round=fill_round,
                           eligible=sorted(eligible), shares=round_shares, needs=round_needs,
                           delta=min_delta, allocation=demand_allocation, residual=residual)

        # Phase 3: Build results for demands in this group
        for demand in group:
            if demand.id not in pending:
                continue  # Already added (local/unreachable)

            path, _, score = pending[demand.id]
            allocated = demand_allocation.get(demand.id, 0.0)
            latency = path_latency(path, G)
            
            if allocated >= demand.bandwidth - 1e-9:
                status = DemandStatus.FULLY_SERVED
                allocated = demand.bandwidth  # Fix floating point
                unserved = 0.0
                explanation = (
                    f"PACAR selected path {' → '.join(path)} "
                    f"(score: {score:.3f}, latency: {latency:.1f}ms). "
                    f"Full bandwidth allocated."
                )
            elif allocated > 0:
                status = DemandStatus.PARTIALLY_SERVED
                unserved = demand.bandwidth - allocated
                explanation = (
                    f"PACAR selected path {' → '.join(path)} "
                    f"(score: {score:.3f}, latency: {latency:.1f}ms). "
                    f"Partial allocation: {allocated:.1f}/{demand.bandwidth:.1f} Mbps. "
                    f"Bottleneck limited available bandwidth."
                )
            else:
                status = DemandStatus.UNSERVED_CONGESTION
                unserved = demand.bandwidth
                explanation = (
                    f"Path {' → '.join(path)} found but no residual capacity available. "
                    f"All links fully utilized by higher-priority or earlier traffic."
                )

            results.append(demand.model_copy(update={
                "status": status,
                "allocated_bandwidth": allocated,
                "unserved_bandwidth": unserved,
                "route": path if allocated > 0 else None,
                "explanation": explanation,
            }))

        if trace is not None:
            trace.emit("pacar.group_result", priority=priority_val, results=[
                {"id": r.id, "status": r.status.value, "allocated": r.allocated_bandwidth,
                 "requested": r.bandwidth, "route": r.route,
                 "path": pending[r.id][0] if r.id in pending else None,
                 "score": pending[r.id][2] if r.id in pending else None}
                for r in results if r.id in demand_by_id and r.id in pending],
                residual=residual)

    if trace is not None:
        trace.emit("pacar.done", results=[
            {"id": r.id, "status": r.status.value, "allocated": r.allocated_bandwidth,
             "requested": r.bandwidth, "route": r.route, "priority": r.priority.value}
            for r in results])
    return results


def run_pacar_allocations(
    demands: dict[str, Any],
    links: dict[str, Any],
    candidate_tunnels: dict[str, list[Any]],
) -> dict[str, dict[str, float]]:
    """
    Produce a tunnel-indexed allocation dictionary:
    flow_id -> {tunnel_id -> mbps}
    using PACAR priority-aware greedy allocation on candidate tunnels.
    Strictly respects shared physical-link capacity in both directions.
    """
    failed_links = set()
    residual: dict[str, float] = {}
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status in ("failed", "flapping"):
            failed_links.add(lid)
            residual[lid] = 0.0
        else:
            cap = float(getattr(link, "capacity", 0.0) if not isinstance(link, dict) else link.get("capacity", 0.0))
            residual[lid] = cap

    allocations: dict[str, dict[str, float]] = {fid: {} for fid in demands}

    # Sort demands by priority (ascending = P1 first), then ID
    sorted_fids = sorted(
        demands.keys(),
        key=lambda fid: (
            getattr(demands[fid], "priority", None).value if hasattr(getattr(demands[fid], "priority", None), "value")
            else value(demands[fid], "priority", 4),
            fid
        )
    )

    for fid in sorted_fids:
        d = demands[fid]
        req_bw = float(getattr(d, "bandwidth", 0.0) if not isinstance(d, dict) else d.get("bandwidth", 0.0))
        if req_bw <= 0:
            continue

        surviving = [t for t in candidate_tunnels.get(fid, []) if t.survives_failure(failed_links) and not tunnel_errors(t, fid, d, links, failed_links)]
        if not surviving:
            continue

        rem_bw = req_bw
        for t in surviving:
            if rem_bw <= 1e-5:
                break
            # Bottleneck on physical links for this tunnel
            avail = min((residual.get(lid, 0.0) for lid in t.link_ids), default=rem_bw)
            if avail > 1e-5:
                alloc_amount = min(rem_bw, avail)
                allocations[fid][t.id] = alloc_amount
                rem_bw -= alloc_amount
                # Deduct from physical links
                for lid in t.link_ids:
                    residual[lid] = max(0.0, residual.get(lid, 0.0) - alloc_amount)

    return allocations
