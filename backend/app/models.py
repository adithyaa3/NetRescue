"""
NetRescue Data Models
=====================
Pydantic models for the complete network simulation.
All bandwidth in Mbps, latency in ms.
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────

class NodeType(str, Enum):
    ROUTER = "router"
    SWITCH = "switch"
    GATEWAY = "gateway"
    SERVER = "server"
    ENDPOINT = "endpoint"


class Priority(int, Enum):
    """Traffic priority levels – lower number = higher priority."""
    P1_EMERGENCY = 1
    P2_ESSENTIAL = 2
    P3_ACADEMIC = 3
    P4_GENERAL = 4
    P5_BACKGROUND = 5


PRIORITY_LABELS = {
    Priority.P1_EMERGENCY: "Emergency",
    Priority.P2_ESSENTIAL: "Essential Infrastructure",
    Priority.P3_ACADEMIC: "Academic",
    Priority.P4_GENERAL: "General",
    Priority.P5_BACKGROUND: "Background",
}


class TrafficCategory(str, Enum):
    EMERGENCY = "emergency"
    ESSENTIAL = "essential"
    ACADEMIC = "academic"
    GENERAL = "general"
    BACKGROUND = "background"


CATEGORY_TO_PRIORITY = {
    TrafficCategory.EMERGENCY: Priority.P1_EMERGENCY,
    TrafficCategory.ESSENTIAL: Priority.P2_ESSENTIAL,
    TrafficCategory.ACADEMIC: Priority.P3_ACADEMIC,
    TrafficCategory.GENERAL: Priority.P4_GENERAL,
    TrafficCategory.BACKGROUND: Priority.P5_BACKGROUND,
}


class DemandStatus(str, Enum):
    FULLY_SERVED = "fully_served"
    PARTIALLY_SERVED = "partially_served"
    UNSERVED_CONGESTION = "unserved_congestion"
    UNREACHABLE = "unreachable"
    LOCAL = "local"
    INVALID = "invalid"
    PENDING = "pending"


class LinkStatus(str, Enum):
    ACTIVE = "active"
    FAILED = "failed"
    FLAPPING = "flapping"


# ── Node ──────────────────────────────────────────────────────────────

class Node(BaseModel):
    id: str
    name: str
    node_type: NodeType = NodeType.ROUTER
    service_label: Optional[str] = None
    x: float = 0.0
    y: float = 0.0


# ── Link ──────────────────────────────────────────────────────────────

class Link(BaseModel):
    id: str
    source: str
    target: str
    capacity: float = Field(gt=0, description="Capacity in Mbps")
    latency: float = Field(ge=0, description="Latency in ms")
    status: LinkStatus = LinkStatus.ACTIVE
    allocated_load: float = Field(default=0.0, ge=0)
    utilization: float = Field(default=0.0, ge=0)
    flap_count: int = Field(default=0, ge=0)
    last_flap_step: int = Field(default=-1)

    @field_validator("utilization", mode="before")
    @classmethod
    def clamp_utilization(cls, v):
        if v is None:
            return 0.0
        return max(0.0, v)


# ── Traffic Demand ────────────────────────────────────────────────────

class Demand(BaseModel):
    id: str
    source: str
    destination: str
    bandwidth: float = Field(gt=0, description="Requested bandwidth in Mbps")
    category: TrafficCategory = TrafficCategory.GENERAL
    priority: Priority = Priority.P4_GENERAL
    allocated_bandwidth: float = Field(default=0.0, ge=0)
    unserved_bandwidth: float = Field(default=0.0, ge=0)
    route: Optional[list[str]] = None
    status: DemandStatus = DemandStatus.PENDING
    explanation: str = ""


# ── API Schemas ───────────────────────────────────────────────────────

class NodeCreate(BaseModel):
    id: str
    name: str
    node_type: NodeType = NodeType.ROUTER
    service_label: Optional[str] = None
    x: float = 0.0
    y: float = 0.0


class LinkCreate(BaseModel):
    id: str
    source: str
    target: str
    capacity: float = Field(gt=0)
    latency: float = Field(ge=0)


class DemandCreate(BaseModel):
    id: str
    source: str
    destination: str
    bandwidth: float = Field(gt=0)
    category: TrafficCategory = TrafficCategory.GENERAL


class DemandUpdate(BaseModel):
    bandwidth: Optional[float] = Field(default=None, gt=0)
    category: Optional[TrafficCategory] = None
    source: Optional[str] = None
    destination: Optional[str] = None


class TopologyData(BaseModel):
    nodes: list[Node]
    links: list[Link]


class TopologyImport(BaseModel):
    nodes: list[Node]
    links: list[Link]
    demands: list[Demand] = []


class FailureRequest(BaseModel):
    link_ids: list[str]


class WhatIfRequest(BaseModel):
    link_ids: list[str]


class GenerateTopologyRequest(BaseModel):
    preset: str = "small_campus"
    seed: Optional[int] = None


class SimulationEvent(BaseModel):
    step: int
    event_type: str
    description: str
    details: dict = {}


class LinkMetrics(BaseModel):
    id: str
    capacity: float
    allocated_load: float
    utilization: float
    status: str


class DemandMetrics(BaseModel):
    id: str
    source: str
    destination: str
    category: str
    priority: int
    requested: float
    allocated: float
    unserved: float
    status: str
    route: Optional[list[str]] = None


class AlgorithmComparison(BaseModel):
    algorithm: str
    total_requested: float
    total_delivered: float
    total_unserved: float
    critical_delivery_ratio: float
    general_delivery_ratio: float
    avg_latency: float
    max_link_utilization: float
    avg_link_utilization: float
    computation_time_ms: float
    demands: list[DemandMetrics] = []
    link_metrics: list[LinkMetrics] = []


class MetricsResponse(BaseModel):
    version: int
    total_requested: float
    total_delivered: float
    total_unserved: float
    critical_delivery_pct: float
    general_delivery_pct: float
    failed_links: int
    disconnected_demands: int
    active_demands: int
    link_utilizations: list[LinkMetrics]
    demand_details: list[DemandMetrics]
    per_priority: dict = {}
    route_changes: int = 0


class VulnerabilityResult(BaseModel):
    link_id: str
    source: str
    target: str
    vulnerability_score: float
    critical_traffic_lost: float
    total_traffic_lost: float
    demands_affected: int
    explanation: str


class WhatIfResult(BaseModel):
    failed_links: list[str]
    total_delivered_before: float
    total_delivered_after: float
    critical_delivered_before: float
    critical_delivered_after: float
    general_delivered_before: float
    general_delivered_after: float
    unreachable_demands: list[str]
    congested_demands: list[str]
    alternative_routes: dict = {}
    high_risk_links: list[str] = []
    explanation: str = ""


class ExportData(BaseModel):
    topology: TopologyData
    demands: list[Demand]
    events: list[SimulationEvent]
    metrics: Optional[MetricsResponse] = None
    comparison: Optional[dict] = None
