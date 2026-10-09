"""
Simulation Engine
==================
Orchestrates the network resilience platform: topology, candidate paths (A),
protection planning (B), reflex recovery (C), reactive LP (D), PACAR (E),
independent validation (F), guarantee checking (G), and install policy (H).
"""

from __future__ import annotations
import time
from typing import Optional, Any

from app.models import (
    Demand, DemandStatus, Priority, LinkStatus, DemandCreate,
    TrafficCategory, CATEGORY_TO_PRIORITY,
)
from app.state import SimulationState, state as global_state
from app.routing.candidate_paths import generate_all_candidate_tunnels, CandidateTunnel
from app.routing.protection_planner import plan_proactive_protection, ProtectionPlan
from app.routing.reflex import run_reflex_recovery
from app.routing.reactive_lp import solve_reactive_multicommodity_lp
from app.routing.pacar import run_pacar, run_pacar_allocations
from app.routing.baseline import run_baseline
from app.routing.allocation import (
    apply_routing_results, apply_allocation_dict,
    validate_allocation, get_congestion_warnings,
)
from app.routing.validator import validate_allocation_independently
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.install_policy import evaluate_install_policy


def update_simulation_protection_plan(state: SimulationState):
    """Recomputes and verifies proactive protection plan for current state."""
    has_critical = any(
        getattr(d, "priority", None) in (Priority.P1_EMERGENCY, Priority.P2_ESSENTIAL, 1, 2)
        or (isinstance(getattr(d, "priority", None), Priority) and getattr(d, "priority", None).value in (1, 2))
        for d in state.demands.values()
    )

    if not has_critical or not state.links:
        state.protection_plan = None
        return

    plan = plan_proactive_protection(
        critical_demands=state.demands,
        links=state.links,
        candidate_tunnels=state.candidate_tunnels,
        topology_version=state.version,
        demand_version=state.version,
    )
    verify_protection_guarantees(
        plan=plan,
        demands=state.demands,
        links=state.links,
        candidate_tunnels=state.candidate_tunnels,
    )
    state.protection_plan = plan


def simulate(state: SimulationState = None, algorithm: str = "pacar") -> dict:
    """
    Run a full simulation step using the specified algorithm (or reactive LP / PACAR).
    Enforces shared physical-link capacity in both directions.
    """
    if state is None:
        state = global_state

    with state.lock():
        state.save_routes_snapshot()
        step = state.bump_step()
        start_time = time.perf_counter()

        # Step 1: Generate / refresh candidate tunnels (Algorithm A)
        state.candidate_tunnels = generate_all_candidate_tunnels(
            demands=state.demands,
            nodes=state.nodes,
            links=state.links,
            K=5,
            include_failed=True,
        )

        # Step 2: Update protection plan (Algorithms B & G)
        update_simulation_protection_plan(state)

        # Step 3: Execute routing
        if algorithm in ("reactive_lp", "lp"):
            lp_res = solve_reactive_multicommodity_lp(
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                previous_allocations=state.active_allocations,
            )
            if lp_res["success"] and lp_res["allocations"]:
                apply_allocation_dict(state, lp_res["allocations"], state.candidate_tunnels)
            else:
                results = run_pacar(state)
                apply_routing_results(state, results)
        elif algorithm == "baseline":
            results = run_baseline(state)
            apply_routing_results(state, results)
        else:
            # Default: PACAR
            results = run_pacar(state)
            apply_routing_results(state, results)

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        route_changes = state.count_route_changes()
        state.route_changes += route_changes

        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)

        state.add_event(
            "simulation_step",
            f"Simulation step {step} completed using {algorithm}. "
            f"{route_changes} route changes. {len(violations)} violations.",
            {
                "algorithm": algorithm,
                "step": step,
                "route_changes": route_changes,
                "computation_time_ms": round(elapsed_ms, 2),
                "violations": violations,
                "congestion_warnings": [w["link_id"] for w in warnings],
            }
        )

        version = state.bump_version()

        return {
            "version": version,
            "step": step,
            "algorithm": algorithm,
            "route_changes": route_changes,
            "computation_time_ms": round(elapsed_ms, 2),
            "violations": violations,
            "congestion_warnings": warnings,
        }


def handle_link_failure(state: SimulationState, link_id: str) -> dict:
    """
    Fail a link and execute the multi-tiered recovery pipeline:
    1. If failure is covered by verified plan -> Reflex Recovery (O(1) fast response)
    2. Then run Reactive LP Optimizer for remaining bandwidth
    3. If uncovered -> PACAR fallback -> Reactive LP Optimizer
    """
    with state.lock():
        link = state.fail_link(link_id)
        failed_link_ids = {link_id}
        state.add_event(
            "link_failure",
            f"Link {link.source}↔{link.target} ({link_id}) failed.",
            {"link_id": link_id, "source": link.source, "target": link.target},
        )

        # Refresh candidate tunnels marking failed links
        state.candidate_tunnels = generate_all_candidate_tunnels(
            demands=state.demands,
            nodes=state.nodes,
            links=state.links,
            K=5,
            include_failed=True,
        )

        # Check Reflex eligibility
        plan = state.protection_plan
        is_covered = (
            plan is not None
            and plan.verified
            and (f"fail_{link_id}" in plan.covered_scenarios or link_id in plan.covered_scenarios)
        )

        reflex_used = False
        reflex_ms = 0.0

        if is_covered and plan:
            # 1. Reflex Recovery Engine (Algorithm C)
            reflex_out = run_reflex_recovery(
                failed_links=failed_link_ids,
                protection_plan=plan,
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                previous_allocations=state.active_allocations,
            )

            if reflex_out["success"]:
                reflex_used = True
                reflex_ms = reflex_out["runtime_ms"]
                apply_allocation_dict(state, reflex_out["allocations"], state.candidate_tunnels)
                state.add_event(
                    "reflex_recovery",
                    f"Reflex Recovery restored {reflex_out['critical_recovered_mbps']:.1f} Mbps "
                    f"critical traffic ({reflex_ms:.2f}ms).",
                    {
                        "link_id": link_id,
                        "critical_recovered_mbps": reflex_out["critical_recovered_mbps"],
                        "normal_recovered_mbps": reflex_out["normal_recovered_mbps"],
                        "runtime_ms": reflex_ms,
                    }
                )

        if not reflex_used:
            # Uncovered failure: immediate PACAR fallback (Algorithm E)
            pacar_results = run_pacar(state)
            apply_routing_results(state, pacar_results)
            state.add_event(
                "uncovered_failure_fallback",
                f"Link {link_id} failure handled by PACAR fallback (uncovered failure).",
                {"link_id": link_id},
            )

        # 2. Reactive LP Optimization (Algorithm D) to further optimize network
        lp_start = time.perf_counter()
        lp_out = solve_reactive_multicommodity_lp(
            demands=state.demands,
            links=state.links,
            candidate_tunnels=state.candidate_tunnels,
            previous_allocations=state.active_allocations,
        )
        lp_ms = (time.perf_counter() - lp_start) * 1000

        if lp_out["success"]:
            # Evaluate with Install Policy (Algorithm H)
            decision = evaluate_install_policy(
                candidate_allocation=lp_out["allocations"],
                current_allocation=state.active_allocations,
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                expected_version=state.version,
                current_version=state.version,
                trigger_reason="link_failure",
            )
            if decision.should_install:
                apply_allocation_dict(state, lp_out["allocations"], state.candidate_tunnels)
                state.add_event(
                    "reactive_lp_optimization",
                    f"Reactive LP optimized allocation ({lp_ms:.2f}ms). {decision.reason}",
                    {"runtime_ms": round(lp_ms, 2), "decision": decision.decision_type},
                )

        route_changes = state.count_route_changes()
        state.route_changes += route_changes
        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)
        step = state.bump_step()
        version = state.bump_version()

        return {
            "version": version,
            "step": step,
            "event": "link_failure",
            "link_id": link_id,
            "reflex_used": reflex_used,
            "reflex_time_ms": round(reflex_ms, 2),
            "route_changes": route_changes,
            "violations": violations,
            "congestion_warnings": warnings,
        }


def handle_link_restore(state: SimulationState, link_id: str) -> dict:
    """Restore a link, re-optimize traffic, and update protection guarantees."""
    with state.lock():
        link = state.restore_link(link_id)
        event_type = "link_restore"
        msg = f"Link {link.source}↔{link.target} ({link_id}) restored."
        if link.status == LinkStatus.FLAPPING:
            event_type = "link_flapping"
            msg = (
                f"Link {link.source}↔{link.target} ({link_id}) marked as flapping "
                f"(failed {link.flap_count} times in {state.flap_window} steps). "
                f"Excluded for {state.flap_cooldown} steps for stabilization."
            )
        state.add_event(event_type, msg, {
            "link_id": link_id,
            "status": link.status.value,
            "flap_count": link.flap_count,
        })

        # Refresh candidate tunnels
        state.candidate_tunnels = generate_all_candidate_tunnels(
            demands=state.demands,
            nodes=state.nodes,
            links=state.links,
            K=5,
            include_failed=True,
        )

        # Run Reactive LP Optimizer
        lp_out = solve_reactive_multicommodity_lp(
            demands=state.demands,
            links=state.links,
            candidate_tunnels=state.candidate_tunnels,
            previous_allocations=state.active_allocations,
        )
        if lp_out["success"]:
            apply_allocation_dict(state, lp_out["allocations"], state.candidate_tunnels)
        else:
            results = run_pacar(state)
            apply_routing_results(state, results)

        # Update protection plan for restored baseline
        update_simulation_protection_plan(state)

        route_changes = state.count_route_changes()
        state.route_changes += route_changes
        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)
        step = state.bump_step()
        version = state.bump_version()

        return {
            "version": version,
            "step": step,
            "event": event_type,
            "link_id": link_id,
            "route_changes": route_changes,
            "violations": violations,
            "congestion_warnings": warnings,
        }


def handle_batch_failures(state: SimulationState, link_ids: list[str]) -> dict:
    """Fail multiple links and re-simulate once (uncovered multi-failure scenario)."""
    events = []
    with state.lock():
        for link_id in link_ids:
            if link_id not in state.links:
                continue
            link = state.fail_link(link_id)
            state.add_event(
                "link_failure",
                f"Link {link.source}↔{link.target} ({link_id}) failed (batch).",
                {"link_id": link_id, "batch": True},
            )
            events.append(link_id)

    # Re-simulate with PACAR / Reactive LP fallback
    result = simulate(state, algorithm="pacar")
    result["event"] = "batch_failure"
    result["failed_links"] = events
    return result


def add_emergency_demand(state: SimulationState, demand: DemandCreate) -> dict:
    """Add an emergency demand (P1) and re-simulate with preemption."""
    with state.lock():
        d = Demand(
            id=demand.id,
            source=demand.source,
            destination=demand.destination,
            bandwidth=demand.bandwidth,
            category=demand.category,
            priority=CATEGORY_TO_PRIORITY.get(demand.category, Priority.P1_EMERGENCY),
            max_latency=demand.max_latency,
        )
        state.add_demand(d)
        state.add_event(
            "emergency_demand",
            f"Emergency demand {demand.id} added: {demand.source}→{demand.destination} "
            f"({demand.bandwidth} Mbps, {demand.category.value}).",
            {"demand_id": demand.id, "bandwidth": demand.bandwidth},
        )

    result = simulate(state)
    result["event"] = "emergency_demand"
    result["demand_id"] = demand.id
    return result


def reset_simulation(state: SimulationState):
    """Full simulation reset."""
    with state.lock():
        state.reset()


def compare_algorithms(state: SimulationState) -> dict:
    """
    Run PACAR, Reactive LP, and Baseline Dijkstra on identical snapshots of state.
    Neither modifies live state. Returns real measured metrics.
    """
    from app.analysis.metrics import compute_metrics

    # 1. Snapshot for PACAR
    snap_pacar = state.snapshot()
    start = time.perf_counter()
    pacar_results = run_pacar(snap_pacar)
    pacar_time = (time.perf_counter() - start) * 1000
    apply_routing_results(snap_pacar, pacar_results)
    pacar_metrics = compute_metrics(snap_pacar)
    pacar_metrics["computation_time_ms"] = round(pacar_time, 2)
    pacar_metrics["algorithm"] = "PACAR"

    # 2. Snapshot for Reactive LP
    snap_lp = state.snapshot()
    start_lp = time.perf_counter()
    cand_tunnels = generate_all_candidate_tunnels(snap_lp.demands, snap_lp.nodes, snap_lp.links, K=5)
    lp_res = solve_reactive_multicommodity_lp(snap_lp.demands, snap_lp.links, cand_tunnels)
    lp_time = (time.perf_counter() - start_lp) * 1000
    if lp_res["success"] and lp_res["allocations"]:
        apply_allocation_dict(snap_lp, lp_res["allocations"], cand_tunnels)
    else:
        apply_routing_results(snap_lp, pacar_results)
    lp_metrics = compute_metrics(snap_lp)
    lp_metrics["computation_time_ms"] = round(lp_time, 2)
    lp_metrics["algorithm"] = "Reactive Multicommodity LP"

    # 3. Snapshot for Baseline (Dijkstra)
    snap_baseline = state.snapshot()
    start_base = time.perf_counter()
    baseline_results = run_baseline(snap_baseline)
    baseline_time = (time.perf_counter() - start_base) * 1000
    apply_routing_results(snap_baseline, baseline_results)
    baseline_metrics = compute_metrics(snap_baseline)
    baseline_metrics["computation_time_ms"] = round(baseline_time, 2)
    baseline_metrics["algorithm"] = "Baseline (Dijkstra)"

    return {
        "pacar": pacar_metrics,
        "reactive_lp": lp_metrics,
        "baseline": baseline_metrics,
    }
