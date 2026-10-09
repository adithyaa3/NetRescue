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
        if not include_failed and (status == LinkStatus.FAILED or status == "failed"):
            continue

        src = getattr(link, "source", None) or link.get("source")
        dst = getattr(link, "target", None) or link.get("target")
        lat = getattr(link, "latency", 1.0) or link.get("latency", 1.0)
        cap = getattr(link, "capacity", 100.0) or link.get("capacity", 100.0)

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
        return []
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
            continue

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
    if len(candidates) == 1 and len(candidates[0].link_ids) > 0:
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
                        if max_latency is None or max_lat <= 0 or alt_lat <= max_latency:
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


def generate_all_candidate_tunnels(
    demands: dict[str, Any],
    nodes: dict[str, Any],
    links: dict[str, Any],
    K: int = 5,
    include_failed: bool = True,  # Generate on base graph so tunnels can be marked failed
) -> dict[str, list[CandidateTunnel]]:
    """
    Generate candidate tunnels for all demands.
    Returns: flow_id -> list of CandidateTunnel.
    """
    G, edge_to_link = build_candidate_graph(nodes, links, include_failed=include_failed)
    all_tunnels: dict[str, list[CandidateTunnel]] = {}

    failed_link_ids = set()
    for lid, link in links.items():
        status = getattr(link, "status", None) or (link.get("status") if isinstance(link, dict) else None)
        if status == LinkStatus.FAILED or status == "failed":
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

        # Update active status based on surviving links
        for t in tunnels:
            t.is_active = t.survives_failure(failed_link_ids)

        all_tunnels[flow_id] = tunnels

    return all_tunnels
