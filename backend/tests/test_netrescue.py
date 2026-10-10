"""
NetRescue Comprehensive Test Suite
====================================
Tests for routing correctness, capacity invariants, priority protection,
fairness, failure handling, what-if analysis, and API validation.
"""

import pytest
import sys
import os
import copy

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import (
    Node, Link, Demand, NodeType, TrafficCategory, Priority,
    DemandStatus, LinkStatus, CATEGORY_TO_PRIORITY,
    NodeCreate, LinkCreate, DemandCreate,
)
from app.state import SimulationState
from app.routing.baseline import run_baseline, build_graph
from app.routing.pacar import run_pacar
from app.routing.allocation import (
    apply_routing_results, validate_allocation, get_congestion_warnings,
)
from app.simulation.engine import (
    simulate, handle_link_failure, handle_link_restore,
    handle_batch_failures, compare_algorithms,
)
from app.analysis.metrics import compute_metrics
from app.analysis.vulnerability import rank_vulnerabilities
from app.analysis.what_if import run_what_if


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def fresh_state():
    """Create a fresh simulation state."""
    return SimulationState()


@pytest.fixture
def simple_topology(fresh_state):
    """Simple A-B-C linear topology."""
    s = fresh_state
    s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER, x=0, y=0))
    s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER, x=100, y=0))
    s.add_node(Node(id="C", name="C", node_type=NodeType.ROUTER, x=200, y=0))
    s.add_link(Link(id="L1", source="A", target="B", capacity=100, latency=5))
    s.add_link(Link(id="L2", source="B", target="C", capacity=100, latency=5))
    return s


@pytest.fixture
def diamond_topology(fresh_state):
    """Diamond: A-B-D and A-C-D with different capacities."""
    s = fresh_state
    s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER, x=0, y=100))
    s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER, x=100, y=0))
    s.add_node(Node(id="C", name="C", node_type=NodeType.ROUTER, x=100, y=200))
    s.add_node(Node(id="D", name="D", node_type=NodeType.ROUTER, x=200, y=100))
    s.add_link(Link(id="L1", source="A", target="B", capacity=50, latency=5))
    s.add_link(Link(id="L2", source="B", target="D", capacity=50, latency=5))
    s.add_link(Link(id="L3", source="A", target="C", capacity=200, latency=10))
    s.add_link(Link(id="L4", source="C", target="D", capacity=200, latency=10))
    return s


@pytest.fixture
def campus_topology(fresh_state):
    """Larger campus-like topology."""
    s = fresh_state
    nodes = [
        Node(id="GW", name="Gateway", node_type=NodeType.GATEWAY, x=300, y=0),
        Node(id="R1", name="Router 1", node_type=NodeType.ROUTER, x=150, y=150),
        Node(id="R2", name="Router 2", node_type=NodeType.ROUTER, x=450, y=150),
        Node(id="SW1", name="Switch 1", node_type=NodeType.SWITCH, x=75, y=300),
        Node(id="SW2", name="Switch 2", node_type=NodeType.SWITCH, x=300, y=300),
        Node(id="SRV", name="Server", node_type=NodeType.SERVER, x=525, y=300),
    ]
    links = [
        Link(id="L1", source="GW", target="R1", capacity=500, latency=2),
        Link(id="L2", source="GW", target="R2", capacity=500, latency=2),
        Link(id="L3", source="R1", target="R2", capacity=300, latency=3),
        Link(id="L4", source="R1", target="SW1", capacity=200, latency=2),
        Link(id="L5", source="R1", target="SW2", capacity=200, latency=3),
        Link(id="L6", source="R2", target="SW2", capacity=200, latency=2),
        Link(id="L7", source="R2", target="SRV", capacity=200, latency=1),
    ]
    for n in nodes:
        s.add_node(n)
    for l in links:
        s.add_link(l)
    return s


def _add_demand(s, did, src, dst, bw, cat):
    d = Demand(
        id=did, source=src, destination=dst,
        bandwidth=bw, category=cat,
        priority=CATEGORY_TO_PRIORITY[cat],
    )
    s.add_demand(d)
    return d


def _validate_invariants(state):
    """Run all invariant checks and assert no violations."""
    violations = validate_allocation(state)
    assert violations == [], f"Invariant violations: {violations}"
    
    for d in state.demands.values():
        assert d.allocated_bandwidth >= -0.001, f"{d.id}: negative allocation"
        assert d.allocated_bandwidth <= d.bandwidth + 0.001, f"{d.id}: over-allocation"
        total = d.allocated_bandwidth + d.unserved_bandwidth
        assert abs(total - d.bandwidth) < 0.02, (
            f"{d.id}: conservation violated: {d.allocated_bandwidth}+{d.unserved_bandwidth}={total} != {d.bandwidth}"
        )
    
    for link in state.links.values():
        if state.is_link_usable(link.id):
            assert link.allocated_load <= link.capacity + 0.01, (
                f"Link {link.id}: load {link.allocated_load} > capacity {link.capacity}"
            )
        else:
            assert link.allocated_load < 0.01, (
                f"Link {link.id}: failed link has load {link.allocated_load}"
            )


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 1: Initial Configuration Edge Cases
# ═══════════════════════════════════════════════════════════════════════

class TestInitialConfiguration:
    """Tests for edge cases 1-7."""
    
    def test_empty_topology(self, fresh_state):
        """Edge case 1: Empty topology."""
        results = run_pacar(fresh_state)
        assert results == []
    
    def test_isolated_nodes(self, fresh_state):
        """Edge case 2: Isolated nodes with no links."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER))
        _add_demand(s, "D1", "A", "B", 10, TrafficCategory.GENERAL)
        results = run_pacar(s)
        apply_routing_results(s, results)
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
    
    def test_invalid_node_reference(self, fresh_state):
        """Edge case 3: Demand references non-existent node."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        with pytest.raises(ValueError, match="not found"):
            _add_demand(s, "D1", "A", "NONEXISTENT", 10, TrafficCategory.GENERAL)
    
    def test_duplicate_node_id(self, fresh_state):
        """Edge case 5: Duplicate node ID."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        with pytest.raises(ValueError, match="already exists"):
            s.add_node(Node(id="A", name="A2", node_type=NodeType.ROUTER))
    
    def test_duplicate_link_id(self, fresh_state):
        """Edge case 5: Duplicate link ID."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER))
        s.add_node(Node(id="C", name="C", node_type=NodeType.ROUTER))
        s.add_link(Link(id="L1", source="A", target="B", capacity=100, latency=5))
        with pytest.raises(ValueError, match="already exists"):
            s.add_link(Link(id="L1", source="A", target="C", capacity=100, latency=5))
    
    def test_source_equals_destination(self, simple_topology):
        """Edge case 7: Source equals destination."""
        s = simple_topology
        _add_demand(s, "D1", "A", "A", 10, TrafficCategory.GENERAL)
        results = run_pacar(s)
        apply_routing_results(s, results)
        assert s.demands["D1"].status == DemandStatus.LOCAL
        assert s.demands["D1"].allocated_bandwidth == 10
    
    def test_parallel_link_rejected(self, fresh_state):
        """Parallel links between same nodes are rejected."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER))
        s.add_link(Link(id="L1", source="A", target="B", capacity=100, latency=5))
        with pytest.raises(ValueError, match="Parallel link"):
            s.add_link(Link(id="L2", source="A", target="B", capacity=50, latency=3))
    
    def test_self_loop_rejected(self, fresh_state):
        """Self-loops are rejected."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        with pytest.raises(ValueError, match="Self-loops"):
            s.add_link(Link(id="L1", source="A", target="A", capacity=100, latency=5))


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 2: Network Failures
# ═══════════════════════════════════════════════════════════════════════

class TestNetworkFailures:
    """Tests for edge cases 8-16."""
    
    def test_single_link_failure(self, simple_topology):
        """Edge case 8: Single link failure."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 50, TrafficCategory.ESSENTIAL)
        simulate(s)
        _validate_invariants(s)
        
        # Fail the link
        handle_link_failure(s, "L1")
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
        _validate_invariants(s)
    
    def test_multiple_link_failures(self, diamond_topology):
        """Edge case 9: Multiple simultaneous failures."""
        s = diamond_topology
        _add_demand(s, "D1", "A", "D", 30, TrafficCategory.ESSENTIAL)
        simulate(s)
        
        # Fail both paths
        handle_batch_failures(s, ["L1", "L3"])
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
        _validate_invariants(s)
    
    def test_complete_partition(self, diamond_topology):
        """Edge case 10: Complete network partition."""
        s = diamond_topology
        _add_demand(s, "D1", "A", "D", 30, TrafficCategory.ESSENTIAL)
        handle_batch_failures(s, ["L1", "L2", "L3", "L4"])
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
        _validate_invariants(s)
    
    def test_bridge_link_failure(self, simple_topology):
        """Edge case 11: Failure of a bridge link (only path)."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 30, TrafficCategory.ESSENTIAL)
        simulate(s)
        assert s.demands["D1"].status == DemandStatus.FULLY_SERVED
        
        handle_link_failure(s, "L2")  # L2 is bridge between B and C
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
    
    def test_link_recovery(self, simple_topology):
        """Edge case 12: Link recovery restores traffic."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 30, TrafficCategory.ESSENTIAL)
        simulate(s)
        assert s.demands["D1"].status == DemandStatus.FULLY_SERVED
        
        handle_link_failure(s, "L1")
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
        
        handle_link_restore(s, "L1")
        assert s.demands["D1"].status == DemandStatus.FULLY_SERVED
        _validate_invariants(s)
    
    def test_repeated_failure_same_link(self, simple_topology):
        """Edge case 13: Repeated failure of the same link (idempotent)."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 30, TrafficCategory.ESSENTIAL)
        simulate(s)
        
        handle_link_failure(s, "L1")
        handle_link_failure(s, "L1")  # Should be safe
        assert s.links["L1"].status == LinkStatus.FAILED
        _validate_invariants(s)
    
    def test_failure_nonexistent_link(self, simple_topology):
        """Edge case 14: Failure of nonexistent link."""
        s = simple_topology
        with pytest.raises(ValueError, match="not found"):
            s.fail_link("NONEXISTENT")
    
    def test_link_flapping_stabilization(self, simple_topology):
        """Edge case 16: Link flapping triggers stabilization."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 30, TrafficCategory.ESSENTIAL)
        
        # Rapidly fail and restore
        for _ in range(4):
            s.bump_step()
            s.fail_link("L1")
            s.bump_step()
            s.restore_link("L1")
        
        # After enough flaps, link should be marked as flapping
        assert s.links["L1"].status == LinkStatus.FLAPPING
        assert not s.is_link_usable("L1")


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 3: Congestion and Capacity
# ═══════════════════════════════════════════════════════════════════════

class TestCongestionCapacity:
    """Tests for edge cases 17-25."""
    
    def test_shortest_lacks_capacity(self, diamond_topology):
        """Edge case 17: Shortest route lacks bandwidth, longer has more."""
        s = diamond_topology
        # Short path A-B-D: 50 Mbps capacity
        # Long path A-C-D: 200 Mbps capacity
        _add_demand(s, "D1", "A", "D", 100, TrafficCategory.ESSENTIAL)
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        d1 = s.demands["D1"]
        # PACAR should use the longer route with more capacity
        assert d1.allocated_bandwidth == 100
        assert d1.route == ["A", "C", "D"]
        _validate_invariants(s)
    
    def test_partial_allocation_required(self, simple_topology):
        """Edge case 20: Partial allocation when capacity insufficient."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 150, TrafficCategory.GENERAL)
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        d1 = s.demands["D1"]
        assert d1.status == DemandStatus.PARTIALLY_SERVED
        assert d1.allocated_bandwidth <= 100.01  # Link capacity
        assert d1.unserved_bandwidth > 0
        _validate_invariants(s)
    
    def test_multiple_flows_share_bottleneck(self, simple_topology):
        """Edge case 21: Multiple flows share a bottleneck."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 60, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "A", "C", 60, TrafficCategory.ESSENTIAL)
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        total_allocated = (
            s.demands["D1"].allocated_bandwidth +
            s.demands["D2"].allocated_bandwidth
        )
        assert total_allocated <= 100.01
        _validate_invariants(s)
    
    def test_total_demand_exceeds_capacity(self, simple_topology):
        """Edge case 22: Total demand far exceeds network capacity."""
        s = simple_topology
        for i in range(10):
            _add_demand(s, f"D{i}", "A", "C", 50, TrafficCategory.GENERAL)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        # Total demand = 500 Mbps, capacity = 100 Mbps
        total_allocated = sum(d.allocated_bandwidth for d in s.demands.values())
        assert total_allocated <= 100.01
        _validate_invariants(s)
    
    def test_no_link_exceeds_capacity(self, campus_topology):
        """Edge case 44: No link exceeds capacity after routing."""
        s = campus_topology
        for i in range(5):
            _add_demand(s, f"D{i}", "SW1", "SRV", 50, TrafficCategory.GENERAL)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        _validate_invariants(s)


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 4: Traffic Priorities
# ═══════════════════════════════════════════════════════════════════════

class TestTrafficPriorities:
    """Tests for edge cases 26-31."""
    
    def test_critical_vs_general_competition(self, simple_topology):
        """Edge case 26: Critical and general traffic compete."""
        s = simple_topology
        # Link capacity = 100 Mbps
        _add_demand(s, "D_CRIT", "A", "C", 70, TrafficCategory.EMERGENCY)
        _add_demand(s, "D_GEN", "A", "C", 70, TrafficCategory.GENERAL)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        # Emergency should get priority
        assert s.demands["D_CRIT"].allocated_bandwidth >= s.demands["D_GEN"].allocated_bandwidth
        _validate_invariants(s)
    
    def test_emergency_during_congestion(self, simple_topology):
        """Edge case 27: Emergency demand arrives during congestion."""
        s = simple_topology
        _add_demand(s, "D_GEN1", "A", "C", 50, TrafficCategory.GENERAL)
        _add_demand(s, "D_GEN2", "A", "C", 50, TrafficCategory.BACKGROUND)
        
        simulate(s)
        # Network is fully loaded
        
        # Add emergency
        _add_demand(s, "D_EMRG", "A", "C", 40, TrafficCategory.EMERGENCY)
        simulate(s)
        
        # Emergency should be served
        assert s.demands["D_EMRG"].allocated_bandwidth > 0
        _validate_invariants(s)
    
    def test_background_reduced_first(self, simple_topology):
        """Edge case 29: Background traffic reduced before critical."""
        s = simple_topology
        _add_demand(s, "D_CRIT", "A", "C", 60, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D_BG", "A", "C", 60, TrafficCategory.BACKGROUND)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        # Critical should get full or more allocation than background
        assert s.demands["D_CRIT"].allocated_bandwidth >= s.demands["D_BG"].allocated_bandwidth
        _validate_invariants(s)
    
    def test_general_traffic_recovers_after_restore(self, diamond_topology):
        """Edge case 30: General traffic recovers after link restoration."""
        s = diamond_topology
        _add_demand(s, "D_GEN", "A", "D", 30, TrafficCategory.GENERAL)
        simulate(s)
        
        initial_bw = s.demands["D_GEN"].allocated_bandwidth
        
        handle_link_failure(s, "L1")  # Fail short path
        mid_bw = s.demands["D_GEN"].allocated_bandwidth
        
        handle_link_restore(s, "L1")
        restored_bw = s.demands["D_GEN"].allocated_bandwidth
        
        assert restored_bw > 0
        _validate_invariants(s)


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 5: Routing Correctness
# ═══════════════════════════════════════════════════════════════════════

class TestRoutingCorrectness:
    """Tests for edge cases 32-38."""
    
    def test_no_path_exists(self, fresh_state):
        """Edge case 32: No path exists."""
        s = fresh_state
        s.add_node(Node(id="A", name="A", node_type=NodeType.ROUTER))
        s.add_node(Node(id="B", name="B", node_type=NodeType.ROUTER))
        _add_demand(s, "D1", "A", "B", 10, TrafficCategory.GENERAL)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
    
    def test_feasible_longer_path(self, diamond_topology):
        """Edge case 34: Feasible longer path when short is full."""
        s = diamond_topology
        # Fill short path
        _add_demand(s, "D1", "A", "D", 50, TrafficCategory.ESSENTIAL)
        simulate(s)
        
        # Now add more demand
        _add_demand(s, "D2", "A", "D", 30, TrafficCategory.ACADEMIC)
        simulate(s)
        
        # D2 should use the longer path
        d2 = s.demands["D2"]
        assert d2.allocated_bandwidth > 0
        if d2.route:
            assert "C" in d2.route  # Should go via C
        _validate_invariants(s)
    
    def test_deterministic_results(self, campus_topology):
        """Edge case 35: Results must be deterministic."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.GENERAL)
        _add_demand(s, "D2", "SW2", "GW", 30, TrafficCategory.ESSENTIAL)
        
        # Run multiple times
        results1 = run_pacar(s)
        results2 = run_pacar(s)
        
        for r1, r2 in zip(results1, results2):
            assert r1.allocated_bandwidth == r2.allocated_bandwidth
            assert r1.route == r2.route
    
    def test_no_routing_loops(self, campus_topology):
        """Edge case 36: No routing loops."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.GENERAL)
        
        results = run_pacar(s)
        apply_routing_results(s, results)
        
        for d in s.demands.values():
            if d.route and len(d.route) > 1:
                # Check for duplicate nodes = loop
                assert len(d.route) == len(set(d.route)), f"Routing loop in {d.id}: {d.route}"
    
    def test_no_traffic_through_failed_links(self, diamond_topology):
        """Edge case 43: No failed link carries traffic."""
        s = diamond_topology
        _add_demand(s, "D1", "A", "D", 30, TrafficCategory.ESSENTIAL)
        
        handle_link_failure(s, "L1")
        
        for link in s.links.values():
            if link.status == LinkStatus.FAILED:
                assert link.allocated_load < 0.01, (
                    f"Failed link {link.id} has load {link.allocated_load}"
                )
        _validate_invariants(s)


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 6: Reporting and Simulation
# ═══════════════════════════════════════════════════════════════════════

class TestReportingSimulation:
    """Tests for edge cases 39-48."""
    
    def test_no_demands(self, simple_topology):
        """Edge case 39: No traffic demands."""
        results = run_pacar(simple_topology)
        assert results == []
    
    def test_no_failed_links(self, simple_topology):
        """Edge case 40: No failed links."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 50, TrafficCategory.GENERAL)
        simulate(s)
        assert all(l.status == LinkStatus.ACTIVE for l in s.links.values())
    
    def test_all_links_failed(self, simple_topology):
        """Edge case 41: All links failed."""
        s = simple_topology
        _add_demand(s, "D1", "A", "C", 50, TrafficCategory.GENERAL)
        handle_batch_failures(s, ["L1", "L2"])
        
        assert s.demands["D1"].status == DemandStatus.UNREACHABLE
        _validate_invariants(s)
    
    def test_demand_conservation(self, campus_topology):
        """Edge case 42: delivered + unserved = requested."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "SW2", "GW", 80, TrafficCategory.GENERAL)
        
        simulate(s)
        
        for d in s.demands.values():
            total = d.allocated_bandwidth + d.unserved_bandwidth
            assert abs(total - d.bandwidth) < 0.02, (
                f"Conservation violated for {d.id}: {d.allocated_bandwidth} + "
                f"{d.unserved_bandwidth} = {total} ≠ {d.bandwidth}"
            )
    
    def test_baseline_and_pacar_identical_conditions(self, campus_topology):
        """Edge case 45: Both algorithms use identical conditions."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "SW2", "GW", 30, TrafficCategory.GENERAL)
        
        comparison = compare_algorithms(s)
        
        # Both should be computed
        assert "pacar" in comparison
        assert "baseline" in comparison
        
        # Both should have valid results
        for algo in ["pacar", "baseline"]:
            m = comparison[algo]
            assert m["total_delivered"] >= 0
            assert m["total_delivered"] <= m["total_requested"] + 0.01
    
    def test_what_if_does_not_mutate(self, campus_topology):
        """Edge case 46: What-if analysis does not mutate live state."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        simulate(s)
        
        version_before = s.version
        links_before = {lid: l.status for lid, l in s.links.items()}
        
        result = run_what_if(s, ["L1", "L2"])
        
        # State should be unchanged
        for lid, status in links_before.items():
            assert s.links[lid].status == status, (
                f"What-if mutated link {lid}: {status} → {s.links[lid].status}"
            )
    
    def test_import_export_preserves(self, campus_topology):
        """Edge case 47: Import/export preserves topology."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        
        # Export
        nodes = list(s.nodes.values())
        links = list(s.links.values())
        demands = list(s.demands.values())
        
        # Import to new state
        s2 = SimulationState()
        s2.load_topology(nodes, links, demands)
        
        assert len(s2.nodes) == len(s.nodes)
        assert len(s2.links) == len(s.links)
        assert len(s2.demands) == len(s.demands)


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 7: Vulnerability and What-If Analysis
# ═══════════════════════════════════════════════════════════════════════

class TestAnalysis:
    """Tests for vulnerability and what-if analysis."""
    
    def test_vulnerability_ranking(self, campus_topology):
        """Vulnerability analysis produces ranked results."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "SW2", "GW", 30, TrafficCategory.ACADEMIC)
        simulate(s)
        
        vulns = rank_vulnerabilities(s)
        assert len(vulns) > 0
        
        # Scores should be sorted descending
        for i in range(len(vulns) - 1):
            assert vulns[i]["vulnerability_score"] >= vulns[i + 1]["vulnerability_score"]
        
        # All scores should be non-negative
        for v in vulns:
            assert v["vulnerability_score"] >= 0
    
    def test_what_if_results(self, campus_topology):
        """What-if analysis returns meaningful results."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        simulate(s)
        
        result = run_what_if(s, ["L1"])
        
        assert "total_delivered_before" in result
        assert "total_delivered_after" in result
        assert "unreachable_demands" in result
        assert "explanation" in result


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 8: Metrics
# ═══════════════════════════════════════════════════════════════════════

class TestMetrics:
    """Tests for metric correctness."""
    
    def test_metrics_basic(self, campus_topology):
        """Metrics are computed correctly."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "SW2", "GW", 30, TrafficCategory.GENERAL)
        simulate(s)
        
        metrics = compute_metrics(s)
        
        assert metrics["total_requested"] == 80.0
        assert metrics["total_delivered"] <= 80.01
        assert metrics["total_delivered"] >= 0
        assert 0 <= metrics["critical_delivery_pct"] <= 100.01
        assert 0 <= metrics["general_delivery_pct"] <= 100.01
    
    def test_per_priority_stats(self, campus_topology):
        """Per-priority stats are present."""
        s = campus_topology
        _add_demand(s, "D1", "SW1", "SRV", 50, TrafficCategory.ESSENTIAL)
        _add_demand(s, "D2", "SW2", "GW", 30, TrafficCategory.BACKGROUND)
        simulate(s)
        
        metrics = compute_metrics(s)
        assert "per_priority" in metrics
        assert len(metrics["per_priority"]) >= 2


# ═══════════════════════════════════════════════════════════════════════
# Test Suite 9: API Integration (using TestClient)
# ═══════════════════════════════════════════════════════════════════════

class TestAPI:
    """Tests for API endpoints using FastAPI TestClient."""
    
    @pytest.fixture(autouse=True)
    def setup_client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.state import state
        # Reset state before each test
        state.reset()
        self.client = TestClient(app)
        self.state = state
    
    def test_health(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
    
    def test_topology_crud(self):
        # Add node
        r = self.client.post("/nodes", json={
            "id": "A", "name": "Node A", "node_type": "router", "x": 0, "y": 0
        })
        assert r.status_code == 200
        
        # Add another node
        r = self.client.post("/nodes", json={
            "id": "B", "name": "Node B", "node_type": "router", "x": 100, "y": 0
        })
        assert r.status_code == 200
        
        # Add link
        r = self.client.post("/links", json={
            "id": "L1", "source": "A", "target": "B",
            "capacity": 100, "latency": 5
        })
        assert r.status_code == 200
        
        # Get topology
        r = self.client.get("/topology")
        assert r.status_code == 200
        data = r.json()
        assert len(data["nodes"]) == 2
        assert len(data["links"]) == 1
    
    def test_demand_crud(self):
        # Setup topology
        self.client.post("/nodes", json={"id": "A", "name": "A", "node_type": "router"})
        self.client.post("/nodes", json={"id": "B", "name": "B", "node_type": "router"})
        self.client.post("/links", json={
            "id": "L1", "source": "A", "target": "B",
            "capacity": 100, "latency": 5
        })
        
        # Add demand
        r = self.client.post("/demands", json={
            "id": "D1", "source": "A", "destination": "B",
            "bandwidth": 50, "category": "general"
        })
        assert r.status_code == 200
        
        # Get demands
        r = self.client.get("/demands")
        assert r.status_code == 200
        assert len(r.json()["demands"]) == 1
        
        # Delete demand
        r = self.client.delete("/demands/D1")
        assert r.status_code == 200
    
    def test_simulation_flow(self):
        # Load scenario
        r = self.client.post("/topology/generate", json={"preset": "normal_campus"})
        assert r.status_code == 200
        
        # Simulate
        r = self.client.post("/simulate?algorithm=pacar")
        assert r.status_code == 200
        
        # Get metrics
        r = self.client.get("/metrics")
        assert r.status_code == 200
        metrics = r.json()
        assert metrics["total_delivered"] > 0
    
    def test_failure_and_restore(self):
        self.client.post("/topology/generate", json={"preset": "normal_campus"})
        self.client.post("/simulate")
        
        # Fail link
        r = self.client.post("/links/L1/fail")
        assert r.status_code == 200
        
        # Restore
        r = self.client.post("/links/L1/restore")
        assert r.status_code == 200
    
    def test_compare(self):
        self.client.post("/topology/generate", json={"preset": "congested_shortest"})
        r = self.client.post("/compare")
        assert r.status_code == 200
        data = r.json()
        assert "pacar" in data
        assert "baseline" in data
    
    def test_what_if(self):
        self.client.post("/topology/generate", json={"preset": "normal_campus"})
        self.client.post("/simulate")
        
        r = self.client.post("/analyze/what-if", json={"link_ids": ["L1"]})
        assert r.status_code == 200
        assert "total_delivered_before" in r.json()
    
    def test_vulnerabilities(self):
        self.client.post("/topology/generate", json={"preset": "normal_campus"})
        self.client.post("/simulate")
        
        r = self.client.get("/analyze/vulnerabilities")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    
    def test_export(self):
        self.client.post("/topology/generate", json={"preset": "normal_campus"})
        self.client.post("/simulate")
        
        r = self.client.get("/export")
        assert r.status_code == 200
        data = r.json()
        assert "topology" in data
        assert "demands" in data
        assert "metrics" in data
    
    def test_scenarios_list(self):
        r = self.client.get("/scenarios")
        assert r.status_code == 200
        scenarios = r.json()
        assert len(scenarios) >= 9
    
    def test_invalid_node_reference(self):
        r = self.client.post("/demands", json={
            "id": "D1", "source": "NONEXISTENT", "destination": "ALSO_NONEXISTENT",
            "bandwidth": 50, "category": "general"
        })
        assert r.status_code == 400
    
    def test_events(self):
        self.client.post("/topology/generate", json={"preset": "normal_campus"})
        r = self.client.get("/events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
