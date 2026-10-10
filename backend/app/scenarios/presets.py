"""
Predefined Demonstration Scenarios
====================================
Nine preset topologies/traffic configurations for demo and testing.
"""

from __future__ import annotations
from app.models import (
    Node, Link, Demand, NodeType, TrafficCategory,
    CATEGORY_TO_PRIORITY, Priority, LinkStatus,
)


def _make_demand(
    did: str, src: str, dst: str, bw: float, cat: TrafficCategory
) -> Demand:
    return Demand(
        id=did, source=src, destination=dst, bandwidth=bw,
        category=cat, priority=CATEGORY_TO_PRIORITY[cat],
    )


# ═══════════════════════════════════════════════════════════════════════
# Scenario 1: Normal Campus — healthy network, mixed traffic
# ═══════════════════════════════════════════════════════════════════════
def scenario_normal_campus() -> dict:
    nodes = [
        Node(id="GW1", name="Main Gateway", node_type=NodeType.GATEWAY, x=400, y=50),
        Node(id="R1", name="Core Router 1", node_type=NodeType.ROUTER, x=250, y=200),
        Node(id="R2", name="Core Router 2", node_type=NodeType.ROUTER, x=550, y=200),
        Node(id="SW1", name="Building A Switch", node_type=NodeType.SWITCH, x=100, y=350),
        Node(id="SW2", name="Building B Switch", node_type=NodeType.SWITCH, x=400, y=350),
        Node(id="SW3", name="Building C Switch", node_type=NodeType.SWITCH, x=700, y=350),
        Node(id="SRV1", name="Auth Server", node_type=NodeType.SERVER, service_label="Authentication", x=100, y=500),
        Node(id="SRV2", name="LMS Server", node_type=NodeType.SERVER, service_label="Learning Management", x=400, y=500),
        Node(id="SRV3", name="Emergency Server", node_type=NodeType.SERVER, service_label="Emergency Systems", x=700, y=500),
        Node(id="EP1", name="Lab Endpoint", node_type=NodeType.ENDPOINT, x=250, y=500),
    ]
    links = [
        Link(id="L1", source="GW1", target="R1", capacity=1000, latency=1),
        Link(id="L2", source="GW1", target="R2", capacity=1000, latency=1),
        Link(id="L3", source="R1", target="R2", capacity=500, latency=2),
        Link(id="L4", source="R1", target="SW1", capacity=500, latency=2),
        Link(id="L5", source="R1", target="SW2", capacity=500, latency=3),
        Link(id="L6", source="R2", target="SW2", capacity=500, latency=2),
        Link(id="L7", source="R2", target="SW3", capacity=500, latency=2),
        Link(id="L8", source="SW1", target="SRV1", capacity=200, latency=1),
        Link(id="L9", source="SW1", target="EP1", capacity=100, latency=1),
        Link(id="L10", source="SW2", target="SRV2", capacity=200, latency=1),
        Link(id="L11", source="SW3", target="SRV3", capacity=200, latency=1),
    ]
    demands = [
        _make_demand("D1", "EP1", "SRV1", 30, TrafficCategory.ESSENTIAL),
        _make_demand("D2", "EP1", "SRV2", 25, TrafficCategory.ACADEMIC),
        _make_demand("D3", "EP1", "GW1", 40, TrafficCategory.GENERAL),
        _make_demand("D4", "SRV1", "GW1", 20, TrafficCategory.ESSENTIAL),
        _make_demand("D5", "SRV2", "GW1", 15, TrafficCategory.ACADEMIC),
        _make_demand("D6", "SRV3", "GW1", 10, TrafficCategory.EMERGENCY),
        _make_demand("D7", "EP1", "SRV3", 5, TrafficCategory.EMERGENCY),
        _make_demand("D8", "SW2", "GW1", 50, TrafficCategory.BACKGROUND),
    ]
    return {
        "name": "Normal Campus",
        "description": "Healthy campus network with mixed critical and general traffic. All categories should receive service.",
        "nodes": nodes, "links": links, "demands": demands,
    }


# ═══════════════════════════════════════════════════════════════════════
# Scenario 2: Primary Link Failure
# ═══════════════════════════════════════════════════════════════════════
def scenario_primary_failure() -> dict:
    data = scenario_normal_campus()
    data["name"] = "Primary Link Failure"
    data["description"] = (
        "Same as normal campus, but link L1 (Gateway↔Core Router 1) is failed. "
        "Traffic must reroute through R2. Observe changed utilization patterns."
    )
    data["fail_links"] = ["L1"]
    return data


# ═══════════════════════════════════════════════════════════════════════
# Scenario 3: Multiple Link Disaster
# ═══════════════════════════════════════════════════════════════════════
def scenario_multiple_disaster() -> dict:
    data = scenario_normal_campus()
    data["name"] = "Multiple Link Disaster"
    data["description"] = (
        "Links L1, L5, and L7 fail simultaneously. "
        "Some endpoints may become partially disconnected. "
        "Demonstrates network recovery limitations."
    )
    data["fail_links"] = ["L1", "L5", "L7"]
    return data


# ═══════════════════════════════════════════════════════════════════════
# Scenario 4: Congested Shortest Route
# ═══════════════════════════════════════════════════════════════════════
def scenario_congested_shortest() -> dict:
    nodes = [
        Node(id="A", name="Source Node A", node_type=NodeType.ROUTER, x=100, y=250),
        Node(id="B", name="Relay B", node_type=NodeType.ROUTER, x=300, y=100),
        Node(id="C", name="Relay C", node_type=NodeType.ROUTER, x=300, y=400),
        Node(id="D", name="Relay D", node_type=NodeType.ROUTER, x=500, y=400),
        Node(id="E", name="Destination E", node_type=NodeType.ROUTER, x=600, y=250),
    ]
    links = [
        Link(id="L1", source="A", target="B", capacity=50, latency=5),    # Short route
        Link(id="L2", source="B", target="E", capacity=50, latency=5),    # Short route
        Link(id="L3", source="A", target="C", capacity=200, latency=10),  # Long route
        Link(id="L4", source="C", target="D", capacity=200, latency=10),  # Long route
        Link(id="L5", source="D", target="E", capacity=200, latency=10),  # Long route
    ]
    demands = [
        _make_demand("D1", "A", "E", 40, TrafficCategory.ESSENTIAL),
        _make_demand("D2", "A", "E", 30, TrafficCategory.ACADEMIC),
        _make_demand("D3", "A", "E", 80, TrafficCategory.GENERAL),
    ]
    return {
        "name": "Congested Shortest Route",
        "description": (
            "Short path A→B→E has 50 Mbps capacity (10ms). "
            "Longer path A→C→D→E has 200 Mbps capacity (30ms). "
            "Total demand is 150 Mbps. PACAR should route excess via the longer path."
        ),
        "nodes": nodes, "links": links, "demands": demands,
    }


# ═══════════════════════════════════════════════════════════════════════
# Scenario 5: Emergency Preemption
# ═══════════════════════════════════════════════════════════════════════
def scenario_emergency_preemption() -> dict:
    nodes = [
        Node(id="GW", name="Gateway", node_type=NodeType.GATEWAY, x=400, y=50),
        Node(id="R1", name="Router 1", node_type=NodeType.ROUTER, x=200, y=200),
        Node(id="R2", name="Router 2", node_type=NodeType.ROUTER, x=600, y=200),
        Node(id="SRV", name="Server", node_type=NodeType.SERVER, x=400, y=350),
        Node(id="EP", name="Endpoint", node_type=NodeType.ENDPOINT, x=400, y=500),
    ]
    links = [
        Link(id="L1", source="GW", target="R1", capacity=200, latency=2),
        Link(id="L2", source="GW", target="R2", capacity=200, latency=2),
        Link(id="L3", source="R1", target="SRV", capacity=100, latency=3),
        Link(id="L4", source="R2", target="SRV", capacity=100, latency=3),
        Link(id="L5", source="SRV", target="EP", capacity=150, latency=1),
    ]
    demands = [
        _make_demand("D1", "EP", "GW", 60, TrafficCategory.GENERAL),
        _make_demand("D2", "EP", "GW", 50, TrafficCategory.BACKGROUND),
        _make_demand("D3", "EP", "GW", 30, TrafficCategory.GENERAL),
    ]
    return {
        "name": "Emergency Preemption",
        "description": (
            "Network is loaded with general/background traffic. "
            "Inject an emergency demand to see lower-priority traffic reduced."
        ),
        "nodes": nodes, "links": links, "demands": demands,
        "inject_emergency": {
            "id": "D_EMRG", "source": "EP", "destination": "GW",
            "bandwidth": 80, "category": "emergency",
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# Scenario 6: Equal-Priority Competition
# ═══════════════════════════════════════════════════════════════════════
def scenario_equal_priority() -> dict:
    nodes = [
        Node(id="A", name="Source A", node_type=NodeType.ROUTER, x=100, y=250),
        Node(id="B", name="Bridge B", node_type=NodeType.ROUTER, x=350, y=250),
        Node(id="C", name="Dest C", node_type=NodeType.SERVER, x=600, y=150),
        Node(id="D", name="Dest D", node_type=NodeType.SERVER, x=600, y=350),
    ]
    links = [
        Link(id="L1", source="A", target="B", capacity=100, latency=5),
        Link(id="L2", source="B", target="C", capacity=80, latency=3),
        Link(id="L3", source="B", target="D", capacity=80, latency=3),
    ]
    demands = [
        _make_demand("D1", "A", "C", 70, TrafficCategory.ESSENTIAL),
        _make_demand("D2", "A", "D", 70, TrafficCategory.ESSENTIAL),
    ]
    return {
        "name": "Equal-Priority Competition",
        "description": (
            "Two P2-Essential demands (70 Mbps each) share bottleneck link L1 (100 Mbps). "
            "Total demand (140 Mbps) exceeds capacity. "
            "Fairness policy should give each demand 50 Mbps."
        ),
        "nodes": nodes, "links": links, "demands": demands,
    }


# ═══════════════════════════════════════════════════════════════════════
# Scenario 7: Flapping Link
# ═══════════════════════════════════════════════════════════════════════
def scenario_flapping_link() -> dict:
    data = scenario_normal_campus()
    data["name"] = "Flapping Link"
    data["description"] = (
        "Repeatedly fail and restore link L3 (R1↔R2). "
        "After several flaps, the link should be marked as flapping "
        "and excluded for stabilization."
    )
    data["flap_link"] = "L3"
    return data


# ═══════════════════════════════════════════════════════════════════════
# Scenario 8: What-if Disaster Analysis
# ═══════════════════════════════════════════════════════════════════════
def scenario_what_if() -> dict:
    data = scenario_normal_campus()
    data["name"] = "What-if Disaster Analysis"
    data["description"] = (
        "Use what-if analysis to test: what happens if links L1 and L7 fail? "
        "Rank vulnerability of all links. Does NOT modify the live network."
    )
    data["what_if_links"] = ["L1", "L7"]
    return data


# ═══════════════════════════════════════════════════════════════════════
# Scenario 9: PACAR vs Dijkstra
# ═══════════════════════════════════════════════════════════════════════
def scenario_pacar_vs_dijkstra() -> dict:
    data = scenario_congested_shortest()
    data["name"] = "PACAR vs Dijkstra"
    data["description"] = (
        "Compare PACAR and baseline Dijkstra on the congested scenario. "
        "Both algorithms use identical topology, demands, and conditions. "
        "Results may vary — PACAR does NOT always outperform baseline."
    )
    return data


# ═══════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════
SCENARIOS = {
    "normal_campus": scenario_normal_campus,
    "primary_failure": scenario_primary_failure,
    "multiple_disaster": scenario_multiple_disaster,
    "congested_shortest": scenario_congested_shortest,
    "emergency_preemption": scenario_emergency_preemption,
    "equal_priority": scenario_equal_priority,
    "flapping_link": scenario_flapping_link,
    "what_if": scenario_what_if,
    "pacar_vs_dijkstra": scenario_pacar_vs_dijkstra,
}


def get_scenario_list() -> list[dict]:
    """Return metadata for all available scenarios."""
    result = []
    for key, fn in SCENARIOS.items():
        data = fn()
        result.append({
            "id": key,
            "name": data["name"],
            "description": data["description"],
        })
    return result


def load_scenario(scenario_id: str) -> dict:
    """Load a scenario by ID."""
    if scenario_id not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario_id}")
    return SCENARIOS[scenario_id]()
