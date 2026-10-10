"""Independent validation using reconstructed paths and shared physical capacity."""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any, Optional

TOLERANCE = 1e-5


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def tunnel_errors(tunnel, flow_id, demand, links, failed_links=(), tolerance=TOLERANCE):
    """Reconstruct topology and latency; do not trust cached tunnel metadata."""
    errors = []
    tid = value(tunnel, 'id')
    nodes = value(tunnel, 'node_path', [])
    lids = value(tunnel, 'link_ids', [])
    if value(tunnel, 'flow_id') != flow_id:
        errors.append(f"Tunnel '{tid}' belongs to another flow")
    if not nodes or nodes[0] != value(demand, 'source') or nodes[-1] != value(demand, 'destination'):
        errors.append(f"Tunnel '{tid}' endpoints do not match flow")
    if len(nodes) != len(set(nodes)) or len(lids) != len(set(lids)):
        errors.append(f"Tunnel '{tid}' is not a simple path")
    if len(lids) != max(0, len(nodes)-1):
        errors.append(f"Tunnel '{tid}' path/link count mismatch")
    latency = 0.0
    for i, lid in enumerate(lids):
        if lid not in links:
            errors.append(f"Tunnel '{tid}' references unknown physical link '{lid}'")
            continue
        link = links[lid]
        if i+1 >= len(nodes) or {nodes[i], nodes[i+1]} != {value(link, 'source'), value(link, 'target')}:
            errors.append(f"Tunnel '{tid}' has disconnected or incorrectly mapped edge '{lid}'")
        if lid in failed_links:
            errors.append(f"Tunnel '{tid}' traverses failed link '{lid}'")
        latency += value(link, 'latency', 0)
    cached = value(tunnel, 'latency', 0)
    if not math.isfinite(cached) or abs(cached-latency) > tolerance:
        errors.append(f"Tunnel '{tid}' latency metadata does not match physical path")
    limit = value(demand, 'max_latency')
    if limit is not None and limit > 0 and latency > limit+tolerance:
        errors.append(f"Tunnel '{tid}' exceeds flow latency limit")
    return errors


@dataclass
class ValidationResult:
    is_valid: bool
    violations: list[str] = field(default_factory=list)
    link_loads: dict[str, float] = field(default_factory=dict)
    total_delivered: float = 0.0
    total_unserved: float = 0.0

    def add_violation(self, msg):
        self.violations.append(msg)
        self.is_valid = False


def validate_allocation_independently(allocations, demands, links, tunnels,
                                      expected_version=None, current_version=None,
                                      failed_links=None, tolerance=TOLERANCE):
    result = ValidationResult(True, link_loads={lid: 0.0 for lid in links})
    if expected_version is not None and expected_version != current_version:
        result.add_violation(f'Version mismatch: {expected_version} != {current_version}')
    unavailable = {lid for lid, link in links.items() if value(link, 'status') in ('failed', 'flapping')}
    failed_links = unavailable | set(failed_links or ())
    delivered = {fid: 0.0 for fid in demands}
    for fid, talloc in allocations.items():
        if fid not in demands:
            result.add_violation(f"Unknown flow ID '{fid}'")
            continue
        for tid, rate in talloc.items():
            if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < -tolerance:
                result.add_violation(f"Flow '{fid}' tunnel '{tid}' rate must be finite and nonnegative")
                continue
            if tid not in tunnels:
                result.add_violation(f"Unknown tunnel '{tid}'")
                continue
            tunnel = tunnels[tid]
            errors = tunnel_errors(tunnel, fid, demands[fid], links, failed_links, tolerance)
            for err in errors:
                result.add_violation(err)
            rate = max(0.0, rate)
            for lid in value(tunnel, 'link_ids', []):
                if lid in result.link_loads:
                    result.link_loads[lid] += rate
            delivered[fid] += rate
    for fid, demand in demands.items():
        requested = value(demand, 'bandwidth', 0.0)
        if delivered[fid] > requested+tolerance:
            result.add_violation(f"Flow '{fid}' allocation exceeds demand")
        result.total_delivered += delivered[fid]
        result.total_unserved += max(0.0, requested-delivered[fid])
    for lid, load in result.link_loads.items():
        if load > value(links[lid], 'capacity', 0.0)+tolerance:
            result.add_violation(f"Physical link '{lid}' capacity exceeded: {load}")
    return result
