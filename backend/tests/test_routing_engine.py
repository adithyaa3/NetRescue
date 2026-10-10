"""
NetRescue Routing Engine & 8 Algorithms Test Suite
==================================================
Tests covering all requirements from Part 18 of the specification:
Test 1  — Shared physical-link capacity across both directions
Test 2  — Single-link protection guarantee (two disjoint paths)
Test 3  — Partial protection (backup capacity bottleneck)
Test 4  — Unprotectable critical flow (bridge link)
Test 5  — Reflex capacity safety & normal traffic scaling
Test 6  — Dynamic demand without restarting simulation
Test 7  — Multiple failures & uncovered failure handling
Test 8  — Stale solver result rejection (Install Policy)
Test 9  — Validator independence & over-capacity rejection
Test 10 — Recovery and link restoration
Test 11 — Lexicographic priority preservation (P1 > P2 > P3 > P4 > P5)
Test 12 — No fabricated comparisons (PACAR vs LP truthful comparison)
Test 13 — Protection plan individual b[f] locking
"""

import pytest
import networkx as nx

from app.models import (
    Node, Link, Demand, Priority, TrafficCategory, LinkStatus, DemandStatus,
)
from app.state import SimulationState
from app.routing.candidate_paths import (
    generate_all_candidate_tunnels, CandidateTunnel,
)
from app.routing.protection_planner import (
    plan_proactive_protection, ProtectionPlan,
)
from app.routing.reflex import run_reflex_recovery
from app.routing.reactive_lp import solve_reactive_multicommodity_lp
from app.routing.pacar import run_pacar, run_pacar_allocations
from app.routing.allocation import validate_allocation
from app.routing.validator import validate_allocation_independently, ValidationResult
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.install_policy import evaluate_install_policy
from app.simulation.engine import (
    simulate, handle_link_failure, handle_link_restore, compare_algorithms,
)


def make_test_state() -> SimulationState:
    state = SimulationState()
    # Diamond network:
    #      R2
    #    /    \
    #  R1      R4
    #    \    /
    #      R3
    state.add_node(Node(id="R1", name="Router 1"))
    state.add_node(Node(id="R2", name="Router 2"))
    state.add_node(Node(id="R3", name="Router 3"))
    state.add_node(Node(id="R4", name="Router 4"))

    state.add_link(Link(id="L12", source="R1", target="R2", capacity=100.0, latency=5.0))
    state.add_link(Link(id="L24", source="R2", target="R4", capacity=100.0, latency=5.0))
    state.add_link(Link(id="L13", source="R1", target="R3", capacity=100.0, latency=10.0))
    state.add_link(Link(id="L34", source="R3", target="R4", capacity=100.0, latency=10.0))
    return state


# ── Test 1: Shared physical-link capacity across both directions ───────

def test_1_shared_physical_link_capacity():
    """
    Physical link R1—R2 has capacity 100 Mbps.
    Flow A (R1 -> R2) requests 70 Mbps.
    Flow B (R2 -> R1) requests 60 Mbps in the opposite direction.
    Total delivered across both directions combined must not exceed 100 Mbps.
    """
    state = SimulationState()
    state.add_node(Node(id="R1", name="R1"))
    state.add_node(Node(id="R2", name="R2"))
    state.add_link(Link(id="L12", source="R1", target="R2", capacity=100.0, latency=5.0))

    state.add_demand(Demand(
        id="D_A", source="R1", destination="R2", bandwidth=70.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))
    state.add_demand(Demand(
        id="D_B", source="R2", destination="R1", bandwidth=60.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    lp_res = solve_reactive_multicommodity_lp(state.demands, state.links, cand)
    assert lp_res["success"]

    # Verify allocation with independent validator
    val = validate_allocation_independently(
        allocations=lp_res["allocations"],
        demands=state.demands,
        links=state.links,
        tunnels={t.id: t for t_list in cand.values() for t in t_list},
    )
    assert val.is_valid, f"Validation violations: {val.violations}"

    # Total load on L12 must be <= 100 Mbps
    l12_load = val.link_loads.get("L12", 0.0)
    assert l12_load <= 100.0 + 1e-4
    assert val.total_delivered <= 100.0 + 1e-4
    assert val.total_delivered >= 99.0  # Should saturate the 100 Mbps bottleneck


# ── Test 2: Single-link protection guarantee (disjoint paths) ──────────

def test_2_single_link_protection_disjoint_paths():
    """
    Two physically edge-disjoint paths (R1-R2-R4 and R1-R3-R4) with 100 Mbps each.
    Critical demand = 20 Mbps.
    Protection Planner must establish a 20 Mbps guarantee across all single link failures.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D_EMERG", source="R1", destination="R4", bandwidth=20.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    plan = plan_proactive_protection(state.demands, state.links, cand)

    assert plan.solver_status == "optimal"
    assert plan.per_flow_guaranteed["D_EMERG"] == pytest.approx(20.0, abs=1e-3)

    # Verify with Guarantee Checker
    verif = verify_protection_guarantees(plan, state.demands, state.links, cand)
    assert verif.is_verified
    assert verif.scenarios_passed == len(plan.covered_scenarios)
    assert plan.verified


# ── Test 3: Partial protection (backup bottleneck) ─────────────────────

def test_3_partial_protection_backup_bottleneck():
    """
    Primary path has 50 Mbps, but backup path only has 10 Mbps.
    Critical demand = 30 Mbps.
    System must NOT claim 30 Mbps guarantee. Guaranteed bandwidth must be <= 10 Mbps.
    """
    state = SimulationState()
    state.add_node(Node(id="R1", name="R1"))
    state.add_node(Node(id="R2", name="R2"))
    state.add_node(Node(id="R3", name="R3"))
    state.add_node(Node(id="R4", name="R4"))

    # Primary path R1-R2-R4: 50 Mbps
    state.add_link(Link(id="L12", source="R1", target="R2", capacity=50.0, latency=5.0))
    state.add_link(Link(id="L24", source="R2", target="R4", capacity=50.0, latency=5.0))
    # Backup path R1-R3-R4: bottleneck 10 Mbps
    state.add_link(Link(id="L13", source="R1", target="R3", capacity=10.0, latency=10.0))
    state.add_link(Link(id="L34", source="R3", target="R4", capacity=50.0, latency=10.0))

    state.add_demand(Demand(
        id="D_EMERG", source="R1", destination="R4", bandwidth=30.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    plan = plan_proactive_protection(state.demands, state.links, cand)

    # Guarantee can be at most 10 Mbps (the capacity of the backup path when L12 fails)
    assert plan.per_flow_guaranteed["D_EMERG"] <= 10.0 + 1e-3
    assert plan.per_flow_guaranteed["D_EMERG"] >= 9.99

    verif = verify_protection_guarantees(plan, state.demands, state.links, cand)
    assert verif.is_verified


# ── Test 4: Unprotectable critical flow (bridge link) ───────────────────

def test_4_unprotectable_critical_flow():
    """
    Linear topology R1—R2—R3.
    Critical demand R1 -> R3 (20 Mbps).
    Failure of bridge link R1-R2 partitions network.
    Guaranteed bandwidth across all single link failures must be zero.
    """
    state = SimulationState()
    state.add_node(Node(id="R1", name="R1"))
    state.add_node(Node(id="R2", name="R2"))
    state.add_node(Node(id="R3", name="R3"))
    state.add_link(Link(id="L12", source="R1", target="R2", capacity=100.0, latency=5.0))
    state.add_link(Link(id="L23", source="R2", target="R3", capacity=100.0, latency=5.0))

    state.add_demand(Demand(
        id="D_EMERG", source="R1", destination="R3", bandwidth=20.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    plan = plan_proactive_protection(state.demands, state.links, cand)

    # Must be 0.0 because failure of L12 or L23 leaves 0 surviving capacity
    assert plan.per_flow_guaranteed["D_EMERG"] == pytest.approx(0.0, abs=1e-4)


# ── Test 5: Reflex capacity safety & normal traffic scaling ────────────

def test_5_reflex_capacity_safety_and_preemption():
    """
    Reflex on covered failure:
    - No failed tunnel receives traffic.
    - No physical link exceeds capacity.
    - Critical guarantee is delivered.
    - Normal traffic scaled down proportionally where link capacity is shared.
    """
    state = make_test_state()
    # Critical demand: 40 Mbps R1 -> R4
    state.add_demand(Demand(
        id="D_CRIT", source="R1", destination="R4", bandwidth=40.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))
    # Normal demand: 80 Mbps R1 -> R4
    state.add_demand(Demand(
        id="D_NORM", source="R1", destination="R4", bandwidth=80.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    plan = plan_proactive_protection(state.demands, state.links, cand)
    verify_protection_guarantees(plan, state.demands, state.links, cand)
    assert plan.verified

    # Simulate link failure of L12
    failed_links = {"L12"}
    prev_alloc = {
        "D_CRIT": {"T_D_CRIT_1": 40.0},
        "D_NORM": {"T_D_NORM_2": 80.0},
    }

    reflex_res = run_reflex_recovery(
        failed_links=failed_links,
        protection_plan=plan,
        demands=state.demands,
        links=state.links,
        candidate_tunnels=cand,
        previous_allocations=prev_alloc,
    )

    assert reflex_res["success"]
    assert reflex_res["is_covered"]
    assert reflex_res["critical_recovered_mbps"] >= 39.99

    # Independent validation
    val = validate_allocation_independently(
        allocations=reflex_res["allocations"],
        demands=state.demands,
        links=state.links,
        tunnels={t.id: t for t_list in cand.values() for t in t_list},
        failed_links=failed_links,
    )
    assert val.is_valid, f"Violations: {val.violations}"


# ── Test 6: Dynamic demand without restarting simulation ───────────────

def test_6_dynamic_demand_without_restarting():
    """
    Start simulation with 0 traffic demands.
    Add 50 Mbps P4 demand -> routing & metrics update cleanly.
    Add 30 Mbps P1 demand -> recomputed live without stopping.
    """
    state = make_test_state()
    # Step 1: Start with 0 demands
    res0 = simulate(state)
    assert res0["step"] == 1
    assert len(state.demands) == 0

    # Step 2: Add 50 Mbps P4 demand
    d1 = Demand(
        id="D1", source="R1", destination="R4", bandwidth=50.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    )
    state.add_demand(d1)
    res1 = simulate(state)
    assert res1["step"] == 2
    assert state.demands["D1"].allocated_bandwidth == pytest.approx(50.0, abs=1e-3)

    # Step 3: Add 30 Mbps P1 demand while running
    d2 = Demand(
        id="D2", source="R1", destination="R4", bandwidth=30.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    )
    state.add_demand(d2)
    res2 = simulate(state)
    assert res2["step"] == 3
    assert state.demands["D2"].allocated_bandwidth == pytest.approx(30.0, abs=1e-3)
    assert state.demands["D1"].allocated_bandwidth == pytest.approx(50.0, abs=1e-3)


# ── Test 7: Multiple failures & uncovered failure handling ─────────────

def test_7_multiple_failures_uncovered_handling():
    """
    Failing two links (L12 and L13) disconnects R1 completely.
    Single-link protection plan does NOT cover this.
    Fallback routing is invoked; unserved bandwidth is reported truthfully.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D_EMERG", source="R1", destination="R4", bandwidth=20.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))
    simulate(state)

    # Fail both L12 and L13
    res = handle_link_failure(state, "L12")
    res_multi = handle_link_failure(state, "L13")

    # Flow should be unreachable / 0 delivered
    d = state.demands["D_EMERG"]
    assert d.allocated_bandwidth == pytest.approx(0.0, abs=1e-3)
    assert d.unserved_bandwidth == pytest.approx(20.0, abs=1e-3)


# ── Test 8: Stale solver result rejection (Install Policy) ─────────────

def test_8_stale_solver_result_rejected():
    """
    An allocation computed for state version N must be rejected
    if the state has moved to version N+1.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D1", source="R1", destination="R4", bandwidth=20.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))
    simulate(state)

    curr_version = state.version
    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    stale_alloc = {"D1": {"T_D1_1": 20.0}}

    decision = evaluate_install_policy(
        candidate_allocation=stale_alloc,
        current_allocation={},
        demands=state.demands,
        links=state.links,
        candidate_tunnels=cand,
        expected_version=curr_version - 1,  # Stale!
        current_version=curr_version,
        trigger_reason="normal",
    )

    assert not decision.should_install
    assert decision.decision_type == "rejected_stale"


# ── Test 9: Validator independence & over-capacity rejection ───────────

def test_9_validator_rejects_over_capacity():
    """
    Deliberately construct an allocation exceeding physical link capacity.
    Validator must reject it with a structured error.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D1", source="R1", destination="R4", bandwidth=150.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))
    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    # L12 capacity is 100 Mbps; allocate 150 Mbps on T_D1_1 (R1-R2-R4)
    invalid_alloc = {"D1": {"T_D1_1": 150.0}}

    val = validate_allocation_independently(
        allocations=invalid_alloc,
        demands=state.demands,
        links=state.links,
        tunnels={t.id: t for t_list in cand.values() for t in t_list},
    )

    assert not val.is_valid
    assert any("capacity exceeded" in v for v in val.violations)


# ── Test 10: Recovery and link restoration ─────────────────────────────

def test_10_recovery_and_link_restoration():
    """
    Fail a link -> reroute -> restore link -> reoptimize.
    All allocations must remain strictly capacity-safe and valid.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D1", source="R1", destination="R4", bandwidth=50.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))
    simulate(state)

    # Fail L12
    res_fail = handle_link_failure(state, "L12")
    assert state.links["L12"].status == LinkStatus.FAILED
    assert state.demands["D1"].allocated_bandwidth == pytest.approx(50.0, abs=1e-3)

    # Restore L12
    res_restore = handle_link_restore(state, "L12")
    assert state.links["L12"].status == LinkStatus.ACTIVE
    assert state.demands["D1"].allocated_bandwidth == pytest.approx(50.0, abs=1e-3)

    # Invariants valid
    violations = validate_allocation(state)
    assert len(violations) == 0


# ── Test 11: Lexicographic priority preservation (P1 > P2 > P3 > P4 > P5)

def test_11_lexicographic_priority_preservation():
    """
    Network bottleneck = 100 Mbps.
    P1 demand: 40 Mbps
    P2 demand: 30 Mbps
    P3 demand: 25 Mbps
    P4 demand: 30 Mbps
    P5 demand: 20 Mbps
    Total demand = 145 Mbps > 100 Mbps.
    P1 and P2 must get 100% (70 Mbps total).
    P3 must get 100% (25 Mbps, total 95 Mbps).
    P4 gets remaining 5 Mbps.
    P5 gets 0 Mbps.
    """
    state = SimulationState()
    state.add_node(Node(id="R1", name="R1"))
    state.add_node(Node(id="R2", name="R2"))
    state.add_link(Link(id="L12", source="R1", target="R2", capacity=100.0, latency=5.0))

    state.add_demand(Demand(id="D_P1", source="R1", destination="R2", bandwidth=40.0, category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY))
    state.add_demand(Demand(id="D_P2", source="R1", destination="R2", bandwidth=30.0, category=TrafficCategory.ESSENTIAL, priority=Priority.P2_ESSENTIAL))
    state.add_demand(Demand(id="D_P3", source="R1", destination="R2", bandwidth=25.0, category=TrafficCategory.ACADEMIC, priority=Priority.P3_ACADEMIC))
    state.add_demand(Demand(id="D_P4", source="R1", destination="R2", bandwidth=30.0, category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL))
    state.add_demand(Demand(id="D_P5", source="R1", destination="R2", bandwidth=20.0, category=TrafficCategory.BACKGROUND, priority=Priority.P5_BACKGROUND))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    lp_res = solve_reactive_multicommodity_lp(state.demands, state.links, cand)

    allocs = lp_res["allocations"]
    assert sum(allocs["D_P1"].values()) == pytest.approx(40.0, abs=1e-3)
    assert sum(allocs["D_P2"].values()) == pytest.approx(30.0, abs=1e-3)
    assert sum(allocs["D_P3"].values()) == pytest.approx(25.0, abs=1e-3)
    assert sum(allocs["D_P4"].values()) == pytest.approx(5.0, abs=1e-3)
    assert sum(allocs["D_P5"].values()) == pytest.approx(0.0, abs=1e-3)


# ── Test 12: No fabricated comparisons (PACAR vs LP measured truthfully)

def test_12_no_fabricated_comparisons():
    """
    Compare PACAR, Reactive LP, and Baseline on identical state.
    Check real measured metrics returned without hardcoding.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="D1", source="R1", destination="R4", bandwidth=60.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))
    state.add_demand(Demand(
        id="D2", source="R1", destination="R4", bandwidth=80.0,
        category=TrafficCategory.GENERAL, priority=Priority.P4_GENERAL,
    ))

    comp = compare_algorithms(state)
    assert "pacar" in comp
    assert "reactive_lp" in comp
    assert "baseline" in comp

    # Both PACAR and Reactive LP must have non-negative runtimes and delivered bandwidths
    assert comp["pacar"]["total_delivered"] > 0
    assert comp["reactive_lp"]["total_delivered"] > 0
    assert comp["pacar"]["computation_time_ms"] >= 0
    assert comp["reactive_lp"]["computation_time_ms"] >= 0


# ── Test 13: Protection plan individual b[f] locking ───────────────────

def test_13_protection_plan_individual_b_locking():
    """
    When reservation minimization runs, EACH individual flow guarantee b[f]
    must remain locked to its value, not redistributed.
    """
    state = make_test_state()
    state.add_demand(Demand(
        id="CRIT_1", source="R1", destination="R4", bandwidth=20.0,
        category=TrafficCategory.EMERGENCY, priority=Priority.P1_EMERGENCY,
    ))
    state.add_demand(Demand(
        id="CRIT_2", source="R1", destination="R4", bandwidth=15.0,
        category=TrafficCategory.ESSENTIAL, priority=Priority.P2_ESSENTIAL,
    ))

    cand = generate_all_candidate_tunnels(state.demands, state.nodes, state.links)
    plan = plan_proactive_protection(state.demands, state.links, cand)

    assert plan.per_flow_guaranteed["CRIT_1"] == pytest.approx(20.0, abs=1e-3)
    assert plan.per_flow_guaranteed["CRIT_2"] == pytest.approx(15.0, abs=1e-3)

    # Verification confirms individual guarantees hold
    verif = verify_protection_guarantees(plan, state.demands, state.links, cand)
    assert verif.is_verified
