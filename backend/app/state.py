"""
NetRescue Simulation State
===========================
Central in-memory state for the simulation.
All mutations go through this module to ensure consistency.
Thread-safety is provided by a simple lock for the single-process hackathon setup.
"""

from __future__ import annotations
import threading
import copy
from typing import Optional

from app.models import (
    Node, Link, Demand, SimulationEvent, LinkStatus,
    DemandStatus, TrafficCategory, CATEGORY_TO_PRIORITY, Priority,
)


class SimulationState:
    """Single source of truth for the network simulation."""

    MAX_EVENTS = 5000

    def __init__(self):
        self._lock = threading.RLock()
        self.reset()

    def reset(self):
        """Reset all state to initial empty values."""
        self.nodes: dict[str, Node] = {}
        self.links: dict[str, Link] = {}
        self.demands: dict[str, Demand] = {}
        self.events: list[SimulationEvent] = []
        self.reactivated_links: list[str] = []
        self.version: int = getattr(self, "version", -1) + 1
        self.step: int = 0
        self.route_changes: int = 0
        self._previous_routes: dict[str, Optional[list[str]]] = {}
        # Routing Engine State (Algorithms A-H)
        self.candidate_tunnels: dict[str, list[Any]] = {}
        self.protection_plan: Optional[Any] = None
        self.active_allocations: dict[str, dict[str, float]] = {}
        # Flapping config
        self.flap_threshold: int = 3  # flaps within window triggers stabilization
        self.flap_window: int = 10     # simulation steps
        self.flap_cooldown: int = 5    # steps a flapping link stays excluded

    def lock(self):
        return self._lock

    def bump_version(self):
        self.version += 1
        return self.version

    def bump_step(self):
        """Advance the controller step; returns it.

        Cooldown transitions are controller mutations, never side effects of reads.
        Links whose flap hold-down expired are returned to service here, logged, and
        listed in ``self.reactivated_links`` for the caller of this step.
        """
        self.step += 1
        self.reactivated_links = []
        for lid, link in list(self.links.items()):
            if link.status == LinkStatus.FLAPPING and self.step - link.last_flap_step >= self.flap_cooldown:
                self.links[lid] = link.model_copy(update={"status": LinkStatus.ACTIVE, "flap_count": 0})
                self.reactivated_links.append(lid)
                self.add_event(
                    "flap_hold_down_ended",
                    f"Link {link.source}↔{link.target} ({lid}) completed its {self.flap_cooldown}-step "
                    "flap hold-down and is back in service.",
                    {"link_id": lid},
                )
                self.bump_version()
        return self.step

    def flap_cooldown_remaining(self, link_id: str) -> int:
        link = self.links.get(link_id)
        if link is None or link.status != LinkStatus.FLAPPING:
            return 0
        return max(0, self.flap_cooldown - (self.step - link.last_flap_step))

    def _refresh_candidate_tunnels(self):
        """Keep cached tunnels consistent with the physical links after a topology edit.

        Latency metadata is recomputed; tunnels whose links were deleted or re-pointed
        are dropped. A flow whose installed allocation used a dropped tunnel loses
        that allocation (it must be re-simulated) instead of keeping a phantom route.
        """
        from app.routing.candidate_paths import rebuild_tunnel
        broken = set()
        for fid, tunnels in list(self.candidate_tunnels.items()):
            kept = []
            for t in tunnels:
                rebuilt = rebuild_tunnel(t, self.links)
                if rebuilt is None:
                    if self.active_allocations.get(fid, {}).get(t.id, 0) > 0:
                        broken.add(fid)
                    continue
                kept.append(rebuilt)
            self.candidate_tunnels[fid] = kept
        if broken:
            self._clear_flow_routing(broken)

    def _mark_unreachable_stale(self):
        """A new or re-pointed link can connect demands last computed as unreachable.

        Those demands carry no allocation, so they become pending (to be routed by
        the next simulation) instead of keeping a status that is no longer true.
        """
        for fid, d in list(self.demands.items()):
            if d.status == DemandStatus.UNREACHABLE:
                self.demands[fid] = d.model_copy(update={
                    "status": DemandStatus.PENDING, "route": None, "tunnels": [],
                    "allocated_bandwidth": 0.0, "unserved_bandwidth": d.bandwidth,
                    "explanation": "Topology changed; run simulation to recalculate reachability.",
                })
                self.active_allocations.pop(fid, None)

    def _flows_using_links(self, link_ids):
        affected = set()
        for fid, demand in self.demands.items():
            if any(set(t.link_ids) & set(link_ids)
                   for t in self.candidate_tunnels.get(fid, [])
                   if self.active_allocations.get(fid, {}).get(t.id, 0) > 0):
                affected.add(fid)
            if demand.route:
                edges = {frozenset(pair) for pair in zip(demand.route, demand.route[1:])}
                if any(frozenset((self.links[lid].source, self.links[lid].target)) in edges
                       for lid in link_ids if lid in self.links):
                    affected.add(fid)
        return affected

    def _clear_flow_routing(self, flow_ids):
        """Edited/deleted inputs cannot keep installed allocations from old inputs.

        Unaffected flows retain their allocation. Edited flows await the next
        explicit simulation; this method does not choose new routes.
        """
        for fid in flow_ids:
            self.active_allocations.pop(fid, None)
            self.candidate_tunnels.pop(fid, None)
            if fid in self.demands:
                d = self.demands[fid]
                local = d.source == d.destination
                self.demands[fid] = d.model_copy(update={
                    "route": [d.source] if local else None, "tunnels": [],
                    "allocated_bandwidth": d.bandwidth if local else 0.0,
                    "unserved_bandwidth": 0.0 if local else d.bandwidth,
                    "status": DemandStatus.LOCAL if local else DemandStatus.PENDING,
                    "explanation": "Routing input changed; run simulation to recalculate.",
                })
        loads = {lid: 0.0 for lid in self.links}
        for fid, demand in self.demands.items():
            if fid in self.active_allocations:
                for t in self.candidate_tunnels.get(fid, []):
                    rate = self.active_allocations[fid].get(t.id, 0.0)
                    for lid in t.link_ids:
                        if lid in loads:
                            loads[lid] += rate
            elif demand.route:
                for u, v in zip(demand.route, demand.route[1:]):
                    for lid, link in self.links.items():
                        if {u, v} == {link.source, link.target}:
                            loads[lid] += demand.allocated_bandwidth
        for lid, link in self.links.items():
            load = loads[lid] if link.status == LinkStatus.ACTIVE else 0.0
            self.links[lid] = link.model_copy(update={
                "allocated_load": load, "utilization": 100 * load / link.capacity if link.capacity else 0.0})

    # ── Node operations ──────────────────────────────────────────────

    def add_node(self, node: Node) -> Node:
        node.id = node.id.strip()
        if not node.id:
            raise ValueError("Node ID cannot be empty")
        if node.id in self.nodes:
            raise ValueError(f"Node '{node.id}' already exists")
        self.nodes[node.id] = node
        self.protection_plan = None
        self.bump_version()
        return node

    def remove_node(self, node_id: str):
        if node_id not in self.nodes:
            raise ValueError(f"Node '{node_id}' not found")
        # Remove links connected to this node
        to_remove = [
            lid for lid, l in self.links.items()
            if l.source == node_id or l.target == node_id
        ]
        affected = self._flows_using_links(to_remove)
        for lid in to_remove:
            del self.links[lid]
        # Remove demands referencing this node
        to_remove_d = [
            did for did, d in self.demands.items()
            if d.source == node_id or d.destination == node_id
        ]
        for did in to_remove_d:
            del self.demands[did]
        del self.nodes[node_id]
        for did in to_remove_d:
            self.candidate_tunnels.pop(did, None)
            self.active_allocations.pop(did, None)
        self._clear_flow_routing(affected | set(to_remove_d))
        self._refresh_candidate_tunnels()
        self.protection_plan = None
        self.bump_version()

    def update_node(self, node_id: str, **kwargs) -> Node:
        if node_id not in self.nodes:
            raise ValueError(f"Node '{node_id}' not found")
        node = self.nodes[node_id]
        if set(kwargs) - {"name", "node_type", "service_label", "x", "y"}:
            raise ValueError("Only editable node fields may be updated")
        updated = Node.model_validate({**node.model_dump(), **kwargs})
        self.nodes[node_id] = updated
        # Node fields (name, type, label, canvas position) never affect routing:
        # links and demands reference the immutable node ID. Valid protection
        # reservations therefore stay valid.
        self.bump_version()
        return updated

    # ── Link operations ──────────────────────────────────────────────

    def add_link(self, link: Link) -> Link:
        if not link.id.strip():
            raise ValueError("Link ID cannot be empty")
        if link.id in self.links:
            raise ValueError(f"Link '{link.id}' already exists")
        if link.source not in self.nodes:
            raise ValueError(f"Source node '{link.source}' not found")
        if link.target not in self.nodes:
            raise ValueError(f"Target node '{link.target}' not found")
        if link.source == link.target:
            raise ValueError("Self-loops are not allowed")
        # Check for parallel links
        for existing in self.links.values():
            if (
                (existing.source == link.source and existing.target == link.target) or
                (existing.source == link.target and existing.target == link.source)
            ):
                raise ValueError(
                    f"Parallel link between '{link.source}' and '{link.target}' "
                    f"already exists as '{existing.id}'. Parallel links are not supported."
                )
        self.links[link.id] = link
        self._mark_unreachable_stale()
        self.protection_plan = None
        self.bump_version()
        return link

    def remove_link(self, link_id: str):
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        affected = self._flows_using_links([link_id])
        del self.links[link_id]
        self._clear_flow_routing(affected)
        self._refresh_candidate_tunnels()
        self.protection_plan = None
        self.bump_version()

    def update_link(self, link_id: str, **kwargs) -> Link:
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        link = self.links[link_id]
        if set(kwargs) - {"source", "target", "capacity", "latency"}:
            raise ValueError("Only editable link fields may be updated")
        updated = Link.model_validate({**link.model_dump(), **kwargs})
        if updated.source not in self.nodes or updated.target not in self.nodes:
            raise ValueError("Link endpoints must reference existing nodes")
        if updated.source == updated.target:
            raise ValueError("Self-loops are not allowed")
        for lid, existing in self.links.items():
            if lid != link_id and {existing.source, existing.target} == {updated.source, updated.target}:
                raise ValueError("Parallel links are not supported")
        affected = self._flows_using_links([link_id])
        self.links[link_id] = updated
        self._clear_flow_routing(affected)
        self._refresh_candidate_tunnels()
        self._mark_unreachable_stale()
        self.protection_plan = None
        self.bump_version()
        return updated

    def fail_link(self, link_id: str) -> Link:
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        link = self.links[link_id]
        if link.status == LinkStatus.FAILED:
            return link  # Already failed, idempotent
        current_step = self.step
        # Flap damping counts failures. A failure within flap_window steps of the
        # link's previous state change continues the flap sequence; otherwise the
        # sequence restarts at one failure.
        if link.last_flap_step >= 0 and (current_step - link.last_flap_step) <= self.flap_window:
            flap_count = link.flap_count + 1
        else:
            flap_count = 1
        updated = link.model_copy(update={
            "status": LinkStatus.FAILED,
            "allocated_load": 0.0,
            "utilization": 0.0,
            "flap_count": flap_count,
            "last_flap_step": current_step,
        })
        self.links[link_id] = updated
        self.bump_version()
        return updated

    def restore_link(self, link_id: str) -> Link:
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        link = self.links[link_id]
        if link.status in (LinkStatus.ACTIVE, LinkStatus.FLAPPING):
            return link  # Restoration already applied; preserve cooldown
        current_step = self.step
        flap_count = link.flap_count
        new_status = LinkStatus.ACTIVE
        # A link that failed flap_threshold times within the window is held down
        # (stabilization) for flap_cooldown steps instead of returning to service.
        # A link that then stayed down longer than the window is stable, not flapping.
        recent = link.last_flap_step >= 0 and (current_step - link.last_flap_step) <= self.flap_window
        if flap_count >= self.flap_threshold and recent:
            new_status = LinkStatus.FLAPPING
        elif not recent:
            flap_count = 0
        updated = link.model_copy(update={
            "status": new_status,
            "flap_count": flap_count,
            "last_flap_step": current_step,
        })
        self.links[link_id] = updated
        self.bump_version()
        return updated

    def is_link_usable(self, link_id: str) -> bool:
        """Check if a link can carry traffic (active and not in flapping cooldown)."""
        if link_id not in self.links:
            return False
        link = self.links[link_id]
        return link.status == LinkStatus.ACTIVE

    # ── Demand operations ────────────────────────────────────────────

    def add_demand(self, demand: Demand) -> Demand:
        if not demand.id.strip():
            raise ValueError("Demand ID cannot be empty")
        if demand.id in self.demands:
            raise ValueError(f"Demand '{demand.id}' already exists")
        if demand.source not in self.nodes:
            raise ValueError(f"Source node '{demand.source}' not found")
        if demand.destination not in self.nodes:
            raise ValueError(f"Destination node '{demand.destination}' not found")
        if demand.source == demand.destination:
            demand = demand.model_copy(update={
                "status": DemandStatus.LOCAL,
                "allocated_bandwidth": demand.bandwidth,
                "unserved_bandwidth": 0.0,
                "explanation": "Source and destination are the same node; traffic is local.",
            })
        else:
            # Not routed yet: nothing delivered, everything outstanding.
            demand = demand.model_copy(update={
                "status": DemandStatus.PENDING,
                "allocated_bandwidth": 0.0,
                "unserved_bandwidth": demand.bandwidth,
                "route": None,
                "tunnels": [],
                "explanation": demand.explanation or "Not simulated yet; run simulation to route this demand.",
            })
        # Set priority from category
        demand = demand.model_copy(update={
            "priority": CATEGORY_TO_PRIORITY[demand.category],
        })
        self.demands[demand.id] = demand
        self.protection_plan = None
        self.bump_version()
        return demand

    def update_demand(self, demand_id: str, **kwargs) -> Demand:
        if demand_id not in self.demands:
            raise ValueError(f"Demand '{demand_id}' not found")
        d = self.demands[demand_id]
        if "category" in kwargs and kwargs["category"] is not None:
            kwargs["priority"] = CATEGORY_TO_PRIORITY[kwargs["category"]]
        values = {k: v for k, v in kwargs.items() if v is not None or k == "max_latency"}
        updated = Demand.model_validate({**d.model_dump(), **values})
        if updated.source not in self.nodes or updated.destination not in self.nodes:
            raise ValueError("Demand endpoints must reference existing nodes")
        self.demands[demand_id] = updated
        self._clear_flow_routing([demand_id])
        self.protection_plan = None
        self.bump_version()
        return updated

    def remove_demand(self, demand_id: str):
        if demand_id not in self.demands:
            raise ValueError(f"Demand '{demand_id}' not found")
        del self.demands[demand_id]
        self._clear_flow_routing([demand_id])
        self.protection_plan = None
        self.bump_version()

    # ── Event logging ────────────────────────────────────────────────

    def add_event(self, event_type: str, description: str, details: dict = None):
        event = SimulationEvent(
            step=self.step,
            event_type=event_type,
            description=description,
            details=details or {},
        )
        self.events.append(event)
        if len(self.events) > self.MAX_EVENTS:
            # Bounded history: keep the most recent events (long-running demos).
            del self.events[:len(self.events) - self.MAX_EVENTS]
        return event

    # ── Route tracking ───────────────────────────────────────────────

    def save_routes_snapshot(self):
        """Save current routes before recalculation to detect changes."""
        self._previous_routes = {
            did: (d.route.copy() if d.route else None)
            for did, d in self.demands.items()
        }

    def count_route_changes(self) -> int:
        """Compare current routes with previous snapshot."""
        changes = 0
        for did, d in self.demands.items():
            prev = self._previous_routes.get(did)
            if d.route != prev:
                changes += 1
        return changes

    # ── Snapshot for what-if ─────────────────────────────────────────

    def snapshot(self) -> SimulationState:
        """Create a deep copy for what-if analysis."""
        snap = SimulationState.__new__(SimulationState)
        snap._lock = threading.RLock()
        snap.nodes = copy.deepcopy(self.nodes)
        snap.links = copy.deepcopy(self.links)
        snap.demands = copy.deepcopy(self.demands)
        snap.candidate_tunnels = copy.deepcopy(self.candidate_tunnels)
        snap.protection_plan = copy.deepcopy(self.protection_plan)
        snap.active_allocations = copy.deepcopy(self.active_allocations)
        snap.events = []  # don't copy events for snapshot
        snap.reactivated_links = []
        snap.version = self.version
        snap.step = self.step
        snap.route_changes = 0
        snap._previous_routes = {}
        snap.flap_threshold = self.flap_threshold
        snap.flap_window = self.flap_window
        snap.flap_cooldown = self.flap_cooldown
        return snap

    # ── Bulk load ────────────────────────────────────────────────────

    def load_topology(self, nodes: list[Node], links: list[Link], demands: list[Demand] = None):
        """Load a complete topology, validating everything."""
        # Validate the complete replacement before touching the current network.
        staged = SimulationState()
        for n in nodes:
            staged.add_node(copy.deepcopy(n))
        for l in links:
            staged.add_link(l.model_copy(update={"allocated_load": 0.0, "utilization": 0.0}, deep=True))
        for d in demands or []:
            staged.add_demand(d.model_copy(update={
                "route": None, "tunnels": [], "allocated_bandwidth": 0.0,
                "unserved_bandwidth": d.bandwidth, "status": DemandStatus.PENDING,
                "explanation": "Imported demand; run simulation to calculate routing.",
            }, deep=True))
        with self.lock():
            self.reset()
            self.nodes = staged.nodes
            self.links = staged.links
            self.demands = staged.demands
            self.bump_version()

    # ── Serialization ────────────────────────────────────────────────

    def get_active_links(self) -> list[Link]:
        return [l for l in self.links.values() if self.is_link_usable(l.id)]

    def get_failed_links(self) -> list[Link]:
        return [l for l in self.links.values() if not self.is_link_usable(l.id)]


# ── Global singleton ─────────────────────────────────────────────────
state = SimulationState()
