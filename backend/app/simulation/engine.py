"""
Simulation Engine
==================
Orchestrates the network simulation: topology, routing, failures, recovery.
"""

from __future__ import annotations
import time
from typing import Optional

from app.models import (
    Demand, DemandStatus, Priority, LinkStatus, DemandCreate,
    TrafficCategory, CATEGORY_TO_PRIORITY,
)
from app.state import SimulationState, state as global_state
from app.routing.pacar import run_pacar
from app.routing.baseline import run_baseline
from app.routing.allocation import apply_routing_results, validate_allocation, get_congestion_warnings


def simulate(state: SimulationState = None, algorithm: str = "pacar") -> dict:
    """
    Run a full simulation step.
    1. Save route snapshot.
    2. Run routing algorithm.
    3. Apply results.
    4. Validate invariants.
    5. Check for congestion warnings.
    6. Return summary.
    """
    if state is None:
        state = global_state

    with state.lock():
        state.save_routes_snapshot()
        step = state.bump_step()

        start_time = time.perf_counter()

        if algorithm == "pacar":
            results = run_pacar(state)
        else:
            results = run_baseline(state)

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        apply_routing_results(state, results)

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
    """Fail a link and re-simulate."""
    with state.lock():
        link = state.fail_link(link_id)
        state.add_event(
            "link_failure",
            f"Link {link.source}↔{link.target} ({link_id}) failed.",
            {"link_id": link_id, "source": link.source, "target": link.target},
        )

    result = simulate(state)
    result["event"] = "link_failure"
    result["link_id"] = link_id
    return result


def handle_link_restore(state: SimulationState, link_id: str) -> dict:
    """Restore a link and re-simulate."""
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

    result = simulate(state)
    result["event"] = event_type
    result["link_id"] = link_id
    return result


def handle_batch_failures(state: SimulationState, link_ids: list[str]) -> dict:
    """Fail multiple links and re-simulate once."""
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

    result = simulate(state)
    result["event"] = "batch_failure"
    result["failed_links"] = events
    return result


def add_emergency_demand(state: SimulationState, demand: DemandCreate) -> dict:
    """Add an emergency demand and re-simulate with preemption."""
    with state.lock():
        d = Demand(
            id=demand.id,
            source=demand.source,
            destination=demand.destination,
            bandwidth=demand.bandwidth,
            category=demand.category,
            priority=CATEGORY_TO_PRIORITY[demand.category],
        )
        state.add_demand(d)
        state.add_event(
            "emergency_demand",
            f"Emergency demand {demand.id} added: {demand.source}→{demand.destination} "
            f"({demand.bandwidth} Mbps, {demand.category.value}).",
            {"demand_id": demand.id, "bandwidth": demand.bandwidth},
        )

    # Re-simulate triggers preemption through priority ordering
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
    Run both PACAR and baseline on identical state, return comparison.
    Neither modifies the live state.
    """
    from app.analysis.metrics import compute_metrics

    # Snapshot for PACAR
    snap_pacar = state.snapshot()
    start = time.perf_counter()
    pacar_results = run_pacar(snap_pacar)
    pacar_time = (time.perf_counter() - start) * 1000
    apply_routing_results(snap_pacar, pacar_results)
    pacar_metrics = compute_metrics(snap_pacar)

    # Snapshot for baseline
    snap_baseline = state.snapshot()
    start = time.perf_counter()
    baseline_results = run_baseline(snap_baseline)
    baseline_time = (time.perf_counter() - start) * 1000
    apply_routing_results(snap_baseline, baseline_results)
    baseline_metrics = compute_metrics(snap_baseline)

    pacar_metrics["computation_time_ms"] = round(pacar_time, 2)
    baseline_metrics["computation_time_ms"] = round(baseline_time, 2)
    pacar_metrics["algorithm"] = "PACAR"
    baseline_metrics["algorithm"] = "Baseline (Dijkstra)"

    return {
        "pacar": pacar_metrics,
        "baseline": baseline_metrics,
    }
