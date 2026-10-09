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

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        """Reset all state to initial empty values."""
        self.nodes: dict[str, Node] = {}
        self.links: dict[str, Link] = {}
        self.demands: dict[str, Demand] = {}
        self.events: list[SimulationEvent] = []
        self.version: int = 0
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
        self.step += 1
        return self.step

    # ── Node operations ──────────────────────────────────────────────

    def add_node(self, node: Node) -> Node:
        node.id = node.id.strip()
        if not node.id:
            raise ValueError("Node ID cannot be empty")
        if node.id in self.nodes:
            raise ValueError(f"Node '{node.id}' already exists")
        self.nodes[node.id] = node
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
        self.bump_version()

    def update_node(self, node_id: str, **kwargs) -> Node:
        if node_id not in self.nodes:
            raise ValueError(f"Node '{node_id}' not found")
        node = self.nodes[node_id]
        updated = node.model_copy(update=kwargs)
        self.nodes[node_id] = updated
        self.bump_version()
        return updated

    # ── Link operations ──────────────────────────────────────────────

    def add_link(self, link: Link) -> Link:
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
        self.bump_version()
        return link

    def remove_link(self, link_id: str):
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        del self.links[link_id]
        self.bump_version()

    def update_link(self, link_id: str, **kwargs) -> Link:
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        link = self.links[link_id]
        updated = link.model_copy(update=kwargs)
        self.links[link_id] = updated
        self.bump_version()
        return updated

    def fail_link(self, link_id: str) -> Link:
        if link_id not in self.links:
            raise ValueError(f"Link '{link_id}' not found")
        link = self.links[link_id]
        if link.status == LinkStatus.FAILED:
            return link  # Already failed, idempotent
        current_step = self.step
        flap_count = link.flap_count
        # Check if this is a flap (recently restored then failed again)
        if link.last_flap_step >= 0 and (current_step - link.last_flap_step) <= self.flap_window:
            flap_count += 1
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
        if link.status == LinkStatus.ACTIVE:
            return link  # Already active, idempotent
        current_step = self.step
        flap_count = link.flap_count
        if link.last_flap_step >= 0 and (current_step - link.last_flap_step) <= self.flap_window:
            flap_count += 1
        new_status = LinkStatus.ACTIVE
        # If flapping too much, mark as flapping (stabilization)
        if flap_count >= self.flap_threshold:
            new_status = LinkStatus.FLAPPING
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
        if link.status == LinkStatus.FAILED:
            return False
        if link.status == LinkStatus.FLAPPING:
            # Check cooldown
            if (self.step - link.last_flap_step) < self.flap_cooldown:
                return False
            else:
                # Cooldown expired, reactivate
                self.links[link_id] = link.model_copy(update={
                    "status": LinkStatus.ACTIVE,
                    "flap_count": 0,
                })
                return True
        return True

    # ── Demand operations ────────────────────────────────────────────

    def add_demand(self, demand: Demand) -> Demand:
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
        # Set priority from category
        demand = demand.model_copy(update={
            "priority": CATEGORY_TO_PRIORITY[demand.category],
        })
        self.demands[demand.id] = demand
        self.bump_version()
        return demand

    def update_demand(self, demand_id: str, **kwargs) -> Demand:
        if demand_id not in self.demands:
            raise ValueError(f"Demand '{demand_id}' not found")
        d = self.demands[demand_id]
        if "category" in kwargs and kwargs["category"] is not None:
            kwargs["priority"] = CATEGORY_TO_PRIORITY[kwargs["category"]]
        updated = d.model_copy(update={k: v for k, v in kwargs.items() if v is not None})
        self.demands[demand_id] = updated
        self.bump_version()
        return updated

    def remove_demand(self, demand_id: str):
        if demand_id not in self.demands:
            raise ValueError(f"Demand '{demand_id}' not found")
        del self.demands[demand_id]
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
        snap._lock = threading.Lock()
        snap.nodes = copy.deepcopy(self.nodes)
        snap.links = copy.deepcopy(self.links)
        snap.demands = copy.deepcopy(self.demands)
        snap.events = []  # don't copy events for snapshot
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
        self.reset()
        for n in nodes:
            self.add_node(n)
        for l in links:
            self.add_link(l)
        if demands:
            for d in demands:
                self.add_demand(d)

    # ── Serialization ────────────────────────────────────────────────

    def get_active_links(self) -> list[Link]:
        return [l for l in self.links.values() if self.is_link_usable(l.id)]

    def get_failed_links(self) -> list[Link]:
        return [l for l in self.links.values() if not self.is_link_usable(l.id)]


# ── Global singleton ─────────────────────────────────────────────────
state = SimulationState()
