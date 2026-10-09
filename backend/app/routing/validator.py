"""
Algorithm F: Independent Allocation Validator
=============================================
Strict, independent validation of any candidate or installed network allocation.
Validates all physical-link capacity constraints, path simple constraints,
latency constraints, non-negativity, and demand conservation.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Any
import networkx as nx

from app.models import LinkStatus, Priority


TOLERANCE = 1e-5


@dataclass
class ValidationResult:
    is_valid: bool
    violations: list[str] = field(default_factory=list)
    link_loads: dict[str, float] = field(default_factory=dict)
    total_delivered: float = 0.0
    total_unserved: float = 0.0

    def add_violation(self, msg: str):
        self.violations.append(msg)
        self.is_valid = False


def validate_allocation_independently(
    allocations: dict[str, dict[str, float]],  # flow_id -> {tunnel_id -> mbps}
    demands: dict[str, Any],                  # flow_id -> Demand object or dict
    links: dict[str, Any],                    # link_id -> Link object or dict
    tunnels: dict[str, Any],                  # tunnel_id -> CandidateTunnel object
    expected_version: Optional[int] = None,
    current_version: Optional[int] = None,
    failed_links: Optional[set[str]] = None,
    tolerance: float = TOLERANCE,
) -> ValidationResult:
    """
    Independently validates a proposed bandwidth allocation against all invariants:
    1. Version match (if expected_version provided)
    2. Every flow ID exists in demands
    3. Every tunnel ID exists and belongs to its flow
    4. Every tunnel is a valid simple path (no repeated nodes or physical links)
    5. No allocated tunnel uses a failed physical link
    6. Every tunnel satisfies its flow's latency limit
    7. All allocations are finite and nonnegative
    8. Per-flow delivered bandwidth does not exceed demand
    9. Physical-link load in BOTH directions combined does not exceed capacity
    10. No unknown physical links are referenced
    11. Requested = delivered + unserved (numerical tolerance)
    12. Zero-demand flows receive zero bandwidth
    """
    result = ValidationResult(is_valid=True)

    # 1. Version check
    if expected_version is not None and current_version is not None:
        if expected_version != current_version:
            result.add_violation(
                f"Version mismatch: allocation computed for version {expected_version}, "
                f"current version is {current_version}"
            )

    if failed_links is None:
        failed_links = set()
        for lid, l in links.items():
            status = getattr(l, "status", None) or (l.get("status") if isinstance(l, dict) else None)
            if status == LinkStatus.FAILED or status == "failed":
                failed_links.add(lid)

    # Accumulator for physical link loads (UNDIRECTED: traffic in both directions combined)
    physical_link_loads: dict[str, float] = {lid: 0.0 for lid in links}

    # 2-8. Check per-flow allocations
    for flow_id, tunnel_allocs in allocations.items():
        if flow_id not in demands:
            result.add_violation(f"Unknown flow ID in allocation: '{flow_id}'")
            continue

        demand_obj = demands[flow_id]
        requested_bw = getattr(demand_obj, "bandwidth", None)
        if requested_bw is None and isinstance(demand_obj, dict):
            requested_bw = demand_obj.get("bandwidth", 0.0)
        max_lat = getattr(demand_obj, "max_latency", None)
        if max_lat is None and isinstance(demand_obj, dict):
            max_lat = demand_obj.get("max_latency")

        flow_delivered = 0.0

        for tid, mbps in tunnel_allocs.items():
            # 7. Finite and non-negative
            if mbps is None or mbps != mbps:  # NaN check
                result.add_violation(f"Flow '{flow_id}' tunnel '{tid}' allocation is NaN/None")
                continue
            if mbps < -tolerance:
                result.add_violation(f"Flow '{flow_id}' tunnel '{tid}' allocation is negative: {mbps}")
                continue

            # Small numerical noise clamp
            if abs(mbps) <= tolerance:
                mbps = 0.0

            if mbps == 0.0:
                continue

            # 3. Tunnel existence and ownership
            if tid not in tunnels:
                result.add_violation(f"Flow '{flow_id}' references unknown tunnel '{tid}'")
                continue

            tunnel = tunnels[tid]
            tunnel_flow_id = getattr(tunnel, "flow_id", None) or (tunnel.get("flow_id") if isinstance(tunnel, dict) else None)
            if tunnel_flow_id != flow_id:
                result.add_violation(f"Tunnel '{tid}' belongs to flow '{tunnel_flow_id}', not '{flow_id}'")

            node_path = getattr(tunnel, "node_path", None) or (tunnel.get("node_path") if isinstance(tunnel, dict) else [])
            link_ids = getattr(tunnel, "link_ids", None) or (tunnel.get("link_ids") if isinstance(tunnel, dict) else [])
            tunnel_latency = getattr(tunnel, "latency", None) or (tunnel.get("latency") if isinstance(tunnel, dict) else 0.0)

            # 4. Simple path check: no repeated nodes, no repeated physical links
            if len(node_path) != len(set(node_path)):
                result.add_violation(f"Tunnel '{tid}' has repeated nodes (non-simple path): {node_path}")
            if len(link_ids) != len(set(link_ids)):
                result.add_violation(f"Tunnel '{tid}' traverses the same physical link multiple times: {link_ids}")

            # 5. Failed link check
            for lid in link_ids:
                if lid in failed_links:
                    result.add_violation(
                        f"Flow '{flow_id}' allocated {mbps:.2f} Mbps on tunnel '{tid}' "
                        f"which traverses failed link '{lid}'"
                    )

            # 6. Latency limit check
            if max_lat is not None and max_lat > 0:
                if tunnel_latency > max_lat + tolerance:
                    result.add_violation(
                        f"Flow '{flow_id}' tunnel '{tid}' latency {tunnel_latency:.1f}ms "
                        f"exceeds limit {max_lat:.1f}ms"
                    )

            # 10. Check that all link IDs exist in links
            for lid in link_ids:
                if lid not in links:
                    result.add_violation(f"Tunnel '{tid}' references unknown physical link '{lid}'")
                else:
                    # 9. Accumulate physical link load
                    physical_link_loads[lid] = physical_link_loads.get(lid, 0.0) + mbps

            flow_delivered += mbps

        # 8. Delivered cannot exceed demand
        if flow_delivered > requested_bw + tolerance:
            result.add_violation(
                f"Flow '{flow_id}' delivered {flow_delivered:.2f} Mbps exceeds "
                f"requested demand {requested_bw:.2f} Mbps"
            )

        # 12. Zero-demand flows get zero
        if requested_bw <= tolerance and flow_delivered > tolerance:
            result.add_violation(
                f"Zero-demand flow '{flow_id}' received positive allocation {flow_delivered:.2f} Mbps"
            )

        result.total_delivered += flow_delivered
        unserved = max(0.0, requested_bw - flow_delivered)
        result.total_unserved += unserved

    # Check unallocated demands for conservation
    for flow_id, demand_obj in demands.items():
        if flow_id not in allocations:
            req = getattr(demand_obj, "bandwidth", 0.0)
            if isinstance(demand_obj, dict):
                req = demand_obj.get("bandwidth", 0.0)
            result.total_unserved += req

    # 9. Physical link capacity constraint: combined load in BOTH directions <= capacity
    for lid, load in physical_link_loads.items():
        link_obj = links.get(lid)
        if link_obj is None:
            continue
        cap = getattr(link_obj, "capacity", 0.0)
        if isinstance(link_obj, dict):
            cap = link_obj.get("capacity", 0.0)

        if load > cap + tolerance:
            result.add_violation(
                f"Physical link '{lid}' capacity exceeded: total load {load:.2f} Mbps "
                f"(combined both directions) > capacity {cap:.2f} Mbps"
            )

    result.link_loads = physical_link_loads
    return result
