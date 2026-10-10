"""
Algorithm A: Candidate Path Generator
====================================
Generates diverse, latency-feasible, simple candidate tunnels for each traffic flow.
Uses Yen's K-shortest paths with edge-disjoint preferences and diversity bonuses.
Deterministic tie-breaking.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import itertools
import networkx as nx

from app.models import LinkStatus
from app.routing.validator import value


@dataclass
class CandidateTunnel:
    id: str
    flow_id: str
    node_path: list[str]
    link_ids: list[str]
    latency: float
    is_active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def survives_failure(self, failed_link_ids: set[str]) -> bool:
        """Check if any physical link on this tunnel is failed."""
        return not any(lid in failed_link_ids for lid in self.link_ids)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "flow_id": self.flow_id,
            "node_path": self.node_path,
            "link_ids": self.link_ids,
            "latency": round(self.latency, 2),
            "is_active": self.is_active,
        }


def build_candidate_graph(nodes: dict[str, Any], links: dict[str, Any], include_failed: bool = False) -> tuple[nx.Graph, dict[tuple[str, str], str]]:
    """
    Build NetworkX graph for path search.
    Maps (u, v) and (v, u) to physical link_id.
    """
    G = nx.Graph()
    edge_to_link: dict[tuple[str, str], str] = {}

    for nid in nodes:
        G.add_node(nid)

    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if not include_failed and (status == LinkStatus.FAILED or status in ("failed", "flapping")):
            continue

        src = value(link, "source")
        dst = value(link, "target")
        lat = value(link, "latency", 1.0)
        cap = value(link, "capacity", 100.0)

        G.add_edge(src, dst, link_id=lid, latency=float(lat), capacity=float(cap), weight=float(lat))
        edge_to_link[(src, dst)] = lid
        edge_to_link[(dst, src)] = lid

    return G, edge_to_link


def node_path_to_link_ids(path: list[str], edge_to_link: dict[tuple[str, str], str]) -> list[str]:
    """Convert an ordered sequence of nodes into physical link IDs."""
    link_ids = []
    for i in range(len(path) - 1):
        pair = (path[i], path[i + 1])
        if pair in edge_to_link:
            link_ids.append(edge_to_link[pair])
    return link_ids


def compute_path_latency(path: list[str], G: nx.Graph) -> float:
    """Compute total latency of a node sequence."""
    total = 0.0
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        if G.has_edge(u, v):
            total += G[u][v].get("latency", 0.0)
    return total


def is_simple_path(path: list[str], link_ids: list[str]) -> bool:
    """A tunnel must not visit any node or physical link more than once."""
    if not path or len(link_ids) != len(path) - 1:
        return False
    if len(path) != len(set(path)):
        return False
    if len(link_ids) != len(set(link_ids)):
        return False
    return True


def generate_candidate_tunnels_for_flow(
    flow_id: str,
    source: str,
    destination: str,
    max_latency: Optional[float],
    G: nx.Graph,
    edge_to_link: dict[tuple[str, str], str],
    K: int = 5,
) -> list[CandidateTunnel]:
    """
    Generate up to K diverse, simple, latency-feasible candidate tunnels for a single flow.
    Applies:
    1. Shortest paths by latency (NetworkX shortest_simple_paths)
    2. Edge-disjoint search attempts
    3. Latency limit filtering
    4. Simple path validation
    5. Deduplication
    6. Deterministic ordering
    """
    if not G.has_node(source) or not G.has_node(destination):
        return []
    if source == destination:
        return [CandidateTunnel(f"T_{flow_id}_1", flow_id, [source], [], 0.0)] if K > 0 else []
    if not nx.has_path(G, source, destination):
        return []

    candidates: list[CandidateTunnel] = []
    seen_paths: set[tuple[str, ...]] = set()

    # Generator for simple paths ordered by weight (latency)
    try:
        path_gen = nx.shortest_simple_paths(G, source, destination, weight="weight")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

    # First pass: gather top paths from generator
    for path in path_gen:
        path_tuple = tuple(path)
        if path_tuple in seen_paths:
            continue

        link_ids = node_path_to_link_ids(path, edge_to_link)
        if not is_simple_path(path, link_ids):
            continue

        latency = compute_path_latency(path, G)
        if max_latency is not None and max_latency > 0 and latency > max_latency:
            # Latency limit exceeded
            break

        seen_paths.add(path_tuple)
        t_id = f"T_{flow_id}_{len(candidates) + 1}"
        candidates.append(CandidateTunnel(
            id=t_id,
            flow_id=flow_id,
            node_path=path,
            link_ids=link_ids,
            latency=latency,
            is_active=True,
        ))

        if len(candidates) >= K:
            break

    # Second pass: attempt edge-disjoint path if only 1 path found so far
    if len(candidates) < K and len(candidates) == 1 and len(candidates[0].link_ids) > 0:
        first_links = set(candidates[0].link_ids)
        G_disjoint = G.copy()
        for u, v, data in list(G_disjoint.edges(data=True)):
            if data.get("link_id") in first_links:
                G_disjoint.remove_edge(u, v)

        if G_disjoint.has_node(source) and G_disjoint.has_node(destination) and nx.has_path(G_disjoint, source, destination):
            try:
                alt_path = nx.shortest_path(G_disjoint, source, destination, weight="weight")
                alt_tuple = tuple(alt_path)
                if alt_tuple not in seen_paths:
                    alt_link_ids = node_path_to_link_ids(alt_path, edge_to_link)
                    if is_simple_path(alt_path, alt_link_ids):
                        alt_lat = compute_path_latency(alt_path, G)
                        if max_latency is None or max_latency <= 0 or alt_lat <= max_latency:
                            seen_paths.add(alt_tuple)
                            t_id = f"T_{flow_id}_{len(candidates) + 1}"
                            candidates.append(CandidateTunnel(
                                id=t_id,
                                flow_id=flow_id,
                                node_path=alt_path,
                                link_ids=alt_link_ids,
                                latency=alt_lat,
                                is_active=True,
                                metadata={"disjoint": True},
                            ))
            except Exception:
                pass

    # Sort deterministically by (latency, hop_count, node_path)
    candidates.sort(key=lambda t: (round(t.latency, 3), len(t.node_path), t.node_path))
    # Re-index tunnel IDs cleanly
    for idx, t in enumerate(candidates):
        t.id = f"T_{flow_id}_{idx + 1}"

    return candidates


def rebuild_tunnel(tunnel: CandidateTunnel, links: dict[str, Any]) -> Optional[CandidateTunnel]:
    """Re-derive a known tunnel against the current physical links.

    Returns a copy with recomputed latency, or None when the path no longer exists
    (a link was deleted or its endpoints changed).
    """
    path = list(tunnel.node_path)
    lids = list(tunnel.link_ids)
    if len(lids) != max(0, len(path) - 1):
        return None
    latency = 0.0
    for i, lid in enumerate(lids):
        link = links.get(lid)
        if link is None or {value(link, "source"), value(link, "target")} != {path[i], path[i + 1]}:
            return None
        latency += float(value(link, "latency", 0.0))
    return CandidateTunnel(tunnel.id, tunnel.flow_id, path, lids, latency, tunnel.is_active, dict(tunnel.metadata))


def generate_all_candidate_tunnels(
    demands: dict[str, Any],
    nodes: dict[str, Any],
    links: dict[str, Any],
    K: int = 5,
    include_failed: bool = True,  # Generate on base graph so tunnels can be marked failed
    previous: Optional[dict[str, list[CandidateTunnel]]] = None,
    keep: Optional[set[tuple[str, str]]] = None,
) -> dict[str, list[CandidateTunnel]]:
    """
    Generate candidate tunnels for all demands.
    Returns: flow_id -> list of CandidateTunnel.

    Candidates are the K shortest paths of the full physical graph (stable across
    link failures, so protection reservations keep referring to the same paths)
    plus the K shortest paths of the currently usable graph, so a flow whose
    full-graph candidates all cross a failed link still has every live route the
    optimizers need.

    With ``previous`` (the last generation), a path keeps the tunnel ID it already
    had and new paths never reuse an ID that denoted a different path: an ID in an
    installed allocation or a protection plan always means the same physical path.
    Tunnels listed in ``keep`` ((flow_id, tunnel_id) pairs referenced by installed
    allocations or reservations) are retained while they still exist physically.
    """
    G, edge_to_link = build_candidate_graph(nodes, links, include_failed=include_failed)
    G_live, edge_live = build_candidate_graph(nodes, links, include_failed=False)
    all_tunnels: dict[str, list[CandidateTunnel]] = {}

    failed_link_ids = set()
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status in ("failed", "flapping"):
            failed_link_ids.add(lid)

    for flow_id, demand in demands.items():
        src = getattr(demand, "source", None) or demand.get("source")
        dst = getattr(demand, "destination", None) or demand.get("destination")
        max_lat = getattr(demand, "max_latency", None)
        if max_lat is None and isinstance(demand, dict):
            max_lat = demand.get("max_latency")

        tunnels = generate_candidate_tunnels_for_flow(
            flow_id=flow_id,
            source=src,
            destination=dst,
            max_latency=max_lat,
            G=G,
            edge_to_link=edge_to_link,
            K=K,
        )
        if include_failed and failed_link_ids:
            seen = {tuple(t.node_path) for t in tunnels}
            for t in generate_candidate_tunnels_for_flow(flow_id, src, dst, max_lat, G_live, edge_live, K):
                if tuple(t.node_path) not in seen:
                    seen.add(tuple(t.node_path))
                    tunnels.append(t)
        prev_list = (previous or {}).get(flow_id, [])
        if keep:
            seen = {tuple(t.node_path) for t in tunnels}
            for old in prev_list:
                if (flow_id, old.id) in keep and tuple(old.node_path) not in seen:
                    rebuilt = rebuild_tunnel(old, links)
                    if rebuilt is not None and (max_lat is None or max_lat <= 0 or rebuilt.latency <= max_lat):
                        seen.add(tuple(rebuilt.node_path))
                        tunnels.append(rebuilt)

        tunnels.sort(key=lambda t: (round(t.latency, 3), len(t.node_path), t.node_path))
        if previous is None or flow_id not in previous:
            for idx, t in enumerate(tunnels):
                t.id = f"T_{flow_id}_{idx + 1}"
        else:
            # Stable identity: reuse the previous ID of an identical path; never recycle an ID.
            by_path = {tuple(t.node_path): t.id for t in prev_list}
            used = {t.id for t in prev_list}
            next_idx = 1
            for t in tunnels:
                prev_id = by_path.get(tuple(t.node_path))
                if prev_id is not None:
                    t.id = prev_id
                    continue
                while f"T_{flow_id}_{next_idx}" in used:
                    next_idx += 1
                t.id = f"T_{flow_id}_{next_idx}"
                used.add(t.id)

        # Update active status based on surviving links
        for t in tunnels:
            t.is_active = t.survives_failure(failed_link_ids)

        all_tunnels[flow_id] = tunnels

    return all_tunnels
