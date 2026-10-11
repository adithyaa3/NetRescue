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


def referenced_tunnels(state: SimulationState) -> set[tuple[str, str]]:
    """(flow_id, tunnel_id) pairs used by installed allocations or plan reservations."""
    keep = {(fid, tid) for fid, alloc in state.active_allocations.items() for tid, r in alloc.items() if r > 0}
    plan = state.protection_plan
    if plan is not None:
        keep |= {(fid, tid) for fid, res in plan.per_tunnel_reservations.items() for tid in res}
    return keep


# ── Algorithm Explorer trace helpers (called only when a TraceRecorder is supplied) ──

def _trace_tunnels(state: SimulationState) -> dict:
    return {fid: [t.to_dict() for t in ts] for fid, ts in state.candidate_tunnels.items()}


def _trace_installed(state: SimulationState, trace, label: str) -> None:
    trace.emit(
        "engine.installed", label=label,
        allocations=state.active_allocations,
        tunnels=_trace_tunnels(state),
        demands=[{"id": d.id, "priority": d.priority.value, "source": d.source,
                  "destination": d.destination, "bandwidth": d.bandwidth,
                  "allocated": d.allocated_bandwidth, "unserved": d.unserved_bandwidth,
                  "status": d.status.value, "route": d.route, "tunnels": d.tunnels}
                 for d in state.demands.values()],
        loads={lid: l.allocated_load for lid, l in state.links.items()},
        statuses={lid: l.status.value for lid, l in state.links.items()},
    )


def _trace_plan_summary(state: SimulationState, trace) -> None:
    plan = state.protection_plan
    if plan is None:
        trace.emit("engine.plan_summary", exists=False,
                   has_critical=any(d.priority.value <= 2 for d in state.demands.values()),
                   has_links=bool(state.links))
        return
    trace.emit("engine.plan_summary", exists=True, verified=plan.verified,
               solver_status=plan.solver_status, guarantees=plan.per_flow_guaranteed,
               reservations=plan.per_tunnel_reservations, scenarios=len(plan.covered_scenarios),
               base_unavailable=plan.base_unavailable, failures=plan.verification_failures,
               current_scenario=plan.scenario_for(unavailable_links(state)))


def refresh_candidate_tunnels(state: SimulationState, trace=None):
    """Regenerate candidate tunnels; known paths keep their IDs (see Algorithm A)."""
    state.candidate_tunnels = generate_all_candidate_tunnels(
        demands=state.demands,
        nodes=state.nodes,
        links=state.links,
        K=5,
        include_failed=True,
        previous=state.candidate_tunnels,
        keep=referenced_tunnels(state),
    )
    if trace is not None:
        trace.emit("engine.candidates", tunnels=_trace_tunnels(state), k=5,
                   unavailable=sorted(unavailable_links(state)))


def unavailable_links(state: SimulationState) -> set[str]:
    return {lid for lid in state.links if not state.is_link_usable(lid)}


def enforce_plan_guarantees(state: SimulationState, trace=None) -> bool:
    """Make the installed allocation honour an in-force verified plan.

    Routing heuristics (PACAR, baseline) and the aggregate-per-class LP do not
    know individual guarantees, so they can leave a planned P1/P2 flow below the
    bandwidth the plan promises in the current scenario. In that case the plan's
    reservations are applied with solver-free Reflex, which then keeps the
    existing allocations strictly by priority in the remaining capacity.
    Returns True when the allocation was changed.
    """
    from app.analysis.metrics import delivered_bandwidth
    plan = state.protection_plan
    if plan is None or not plan.verified:
        if trace is not None:
            trace.emit("engine.enforce", outcome="no_verified_plan", plan_exists=plan is not None)
        return False
    unavailable = unavailable_links(state)
    scenario = plan.scenario_for(unavailable)
    if scenario is None:
        if trace is not None:
            trace.emit("engine.enforce", outcome="not_covered", unavailable=sorted(unavailable),
                       base_unavailable=sorted(plan.base_unavailable))
        return False
    short = []
    checked = {} if trace is not None else None
    for fid, g in plan.per_flow_guaranteed.items():
        d = state.demands.get(fid)
        if d is None or g <= 1e-6 or d.status == DemandStatus.PENDING:
            continue
        got = delivered_bandwidth(state, d)
        if checked is not None:
            checked[fid] = {"guarantee": g, "delivered": got}
        if got < g - 1e-6:
            short.append(f"{fid} {got:.2f}<{g:.2f}")
    if not short:
        if trace is not None:
            trace.emit("engine.enforce", outcome="all_met", scenario=scenario, checked=checked)
        return False
    if trace is not None:
        trace.emit("engine.enforce", outcome="short", scenario=scenario, checked=checked, short=short)
    out = run_reflex_recovery(
        failed_links=unavailable,
        protection_plan=plan,
        demands=state.demands,
        links=state.links,
        candidate_tunnels=state.candidate_tunnels,
        previous_allocations=state.active_allocations,
        network_version=state.version,
        trace=trace,
    )
    if not out["success"]:
        state.add_event("guarantee_enforcement_failed",
                        f"Could not apply verified reservations ({out.get('message')}).",
                        {"short_flows": short})
        if trace is not None:
            trace.emit("engine.enforce_result", applied=False, message=out.get("message"))
        return False
    apply_allocation_dict(state, out["allocations"], state.candidate_tunnels)
    if trace is not None:
        trace.emit("engine.enforce_result", applied=True, scenario=scenario)
        _trace_installed(state, trace, "guarantee_enforcement")
    state.add_event(
        "guarantee_enforcement",
        f"Routing left {', '.join(short)} Mbps below the verified guarantee; applied the plan's "
        f"reservations ({scenario}) and kept other traffic by priority.",
        {"short_flows": short, "scenario": scenario},
    )
    return True


def update_simulation_protection_plan(state: SimulationState, trace=None):
    """Recomputes and verifies proactive protection plan for current state."""
    has_critical = any(
        getattr(d, "priority", None) in (Priority.P1_EMERGENCY, Priority.P2_ESSENTIAL, 1, 2)
        or (isinstance(getattr(d, "priority", None), Priority) and getattr(d, "priority", None).value in (1, 2))
        for d in state.demands.values()
    )

    if not has_critical or not state.links:
        state.protection_plan = None
        if trace is not None:
            trace.emit("engine.plan_skipped", has_critical=bool(has_critical), has_links=bool(state.links))
        return

    plan = plan_proactive_protection(
        critical_demands=state.demands,
        links=state.links,
        candidate_tunnels=state.candidate_tunnels,
        topology_version=state.version,
        demand_version=state.version,
        trace=trace,
    )
    if trace is not None:
        trace.set_phase("guarantee_checker")
    verify_protection_guarantees(
        plan=plan,
        demands=state.demands,
        links=state.links,
        candidate_tunnels=state.candidate_tunnels,
        trace=trace,
    )
    state.protection_plan = plan


def simulate(state: SimulationState = None, algorithm: str = "pacar", trace=None) -> dict:
    """
    Run a full simulation step using the specified algorithm (or reactive LP / PACAR).
    Enforces shared physical-link capacity in both directions.
    """
    if state is None:
        state = global_state

    with state.lock():
        step = state.bump_step()
        return _simulate_at_step(state, algorithm, step, trace=trace)


def _simulate_at_step(state: SimulationState, algorithm: str, step: int, trace=None) -> dict:
    """Route all demands at an already-advanced controller step (lock held by caller)."""
    with state.lock():
        state.save_routes_snapshot()
        start_time = time.perf_counter()
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.sim_start", algorithm=algorithm, step=step,
                       unavailable=sorted(unavailable_links(state)),
                       reactivated=list(state.reactivated_links))

        # Step 1: Generate / refresh candidate tunnels (Algorithm A)
        refresh_candidate_tunnels(state, trace=trace)

        # Step 2: Update protection plan (Algorithms B & G)
        update_simulation_protection_plan(state)
        if trace is not None:
            _trace_plan_summary(state, trace)

        # Step 3: Execute routing
        algorithm_used = algorithm
        if algorithm in ("reactive_lp", "lp"):
            # Lexicographic P1..P5 optimum subject to the verified guarantees (commitments).
            plan = state.protection_plan
            floors = None
            if plan is not None and plan.verified and plan.scenario_for(unavailable_links(state)) is not None:
                floors = {fid: g for fid, g in plan.per_flow_guaranteed.items() if g > 1e-9}
            if trace is not None:
                trace.set_phase("reactive_lp")
                trace.emit("engine.lp_floors", floors=floors, plan_exists=plan is not None,
                           plan_verified=bool(plan is not None and plan.verified),
                           covered=bool(plan is not None and plan.scenario_for(unavailable_links(state)) is not None))
            lp_res = solve_reactive_multicommodity_lp(
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                previous_allocations=state.active_allocations,
                protection_floors=floors,
                trace=trace,
            )
            if lp_res["success"] and lp_res["allocations"]:
                apply_allocation_dict(state, lp_res["allocations"], state.candidate_tunnels)
            else:
                algorithm_used = "pacar"
                if trace is not None:
                    trace.set_phase("pacar")
                    trace.emit("engine.lp_fallback", solver_status=lp_res.get("solver_status"))
                results = run_pacar(state, trace=trace)
                apply_routing_results(state, results)
                state.add_event(
                    "lp_fallback",
                    f"Reactive LP could not produce an allocation ({lp_res.get('solver_status')}); "
                    "PACAR allocation installed instead.",
                    {"solver_status": lp_res.get("solver_status")},
                )
        elif algorithm == "baseline":
            if trace is not None:
                trace.set_phase("baseline")
            results = run_baseline(state, trace=trace)
            apply_routing_results(state, results)
        else:
            # Default: PACAR
            if trace is not None:
                trace.set_phase("pacar")
            results = run_pacar(state, trace=trace)
            apply_routing_results(state, results)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        if trace is not None:
            trace.set_phase("controller")
            _trace_installed(state, trace, f"{algorithm_used}_result")
            trace.set_phase("guarantee_enforcement")
        enforce_plan_guarantees(state, trace=trace)

        route_changes = state.count_route_changes()
        state.route_changes += route_changes

        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.sim_done", algorithm=algorithm, algorithm_used=algorithm_used,
                       step=step, route_changes=route_changes, violations=violations,
                       congestion=[w["link_id"] for w in warnings], computation_time_ms=elapsed_ms)

        state.add_event(
            "simulation_step",
            f"Simulation step {step} completed using {algorithm_used}. "
            f"{route_changes} route changes. {len(violations)} violations.",
            {
                "algorithm": algorithm_used,
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
            "algorithm_used": algorithm_used,
            "route_changes": route_changes,
            "computation_time_ms": round(elapsed_ms, 2),
            "violations": violations,
            "congestion_warnings": warnings,
        }


def _noop_result(state: SimulationState, link_id: str, event: str, message: str) -> dict:
    """A request that changes nothing: no step, no event, no recomputation."""
    return {
        "version": state.version,
        "step": state.step,
        "event": event,
        "changed": False,
        "link_id": link_id,
        "message": message,
        "reflex_used": False,
        "reflex_time_ms": 0.0,
        "route_changes": 0,
        "violations": validate_allocation(state),
        "congestion_warnings": get_congestion_warnings(state),
    }


def handle_link_failure(state: SimulationState, link_id: str, trace=None) -> dict:
    """
    Fail a link and execute the multi-tiered recovery pipeline:
    1. If failure is covered by verified plan -> Reflex Recovery (solver-free response)
    2. Then run Reactive LP Optimizer for remaining bandwidth
    3. If uncovered -> PACAR fallback -> Reactive LP Optimizer
    All events of this action carry the step returned in the response.
    """
    with state.lock():
        if link_id not in state.links:
            raise ValueError(f"Link '{link_id}' not found")
        if state.links[link_id].status == LinkStatus.FAILED:
            if trace is not None:
                trace.emit("engine.noop", action="link_failure", link_id=link_id, reason="already_failed")
            return _noop_result(state, link_id, "link_already_failed",
                                f"Link {link_id} is already failed; nothing changed.")
        step = state.bump_step()
        if trace is not None:
            trace.set_phase("controller")
            previous = state.links[link_id]
            trace.emit("engine.failure_start", link_id=link_id, step=step,
                       source=previous.source, target=previous.target,
                       capacity=previous.capacity, load_before=previous.allocated_load,
                       allocations_before=state.active_allocations,
                       tunnels_before=_trace_tunnels(state),
                       loads_before={lid: l.allocated_load for lid, l in state.links.items()},
                       unavailable_before=sorted(unavailable_links(state)),
                       reactivated=list(state.reactivated_links))
        # A hold-down that just expired changed the network the plan was computed
        # for: re-plan for the pre-failure network before handling the failure.
        if state.reactivated_links and state.protection_plan is not None:
            refresh_candidate_tunnels(state)
            update_simulation_protection_plan(state)
            if trace is not None:
                trace.emit("engine.replan_after_hold_down", reactivated=list(state.reactivated_links))
                _trace_plan_summary(state, trace)

        link = state.fail_link(link_id)
        failed_link_ids = unavailable_links(state)
        state.add_event(
            "link_failure",
            f"Link {link.source}↔{link.target} ({link_id}) failed.",
            {"link_id": link_id, "source": link.source, "target": link.target},
        )
        if trace is not None:
            trace.emit("engine.link_failed", link_id=link_id, unavailable=sorted(failed_link_ids),
                       flap_count=link.flap_count)

        # Refresh candidate tunnels marking failed links (IDs stay stable)
        refresh_candidate_tunnels(state, trace=trace)

        state.save_routes_snapshot()

        # Check Reflex eligibility: verified plan and the live failure set is a covered scenario
        plan = state.protection_plan
        scenario = plan.scenario_for(failed_link_ids) if plan is not None else None
        is_covered = plan is not None and plan.verified and scenario is not None
        if trace is not None:
            trace.emit("engine.coverage", plan_exists=plan is not None,
                       plan_verified=bool(plan is not None and plan.verified), scenario=scenario,
                       covered=is_covered, unavailable=sorted(failed_link_ids),
                       base_unavailable=sorted(plan.base_unavailable) if plan is not None else [],
                       guarantees=dict(plan.per_flow_guaranteed) if plan is not None else {})

        reflex_used = False
        reflex_ms = 0.0

        if is_covered and plan:
            # 1. Reflex Recovery Engine (Algorithm C)
            if trace is not None:
                trace.set_phase("reflex")
            reflex_out = run_reflex_recovery(
                failed_links=failed_link_ids,
                protection_plan=plan,
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                previous_allocations=state.active_allocations,
                network_version=state.version,
                trace=trace,
            )

            if reflex_out["success"]:
                reflex_used = True
                reflex_ms = reflex_out["runtime_ms"]
                apply_allocation_dict(state, reflex_out["allocations"], state.candidate_tunnels)
                if trace is not None:
                    _trace_installed(state, trace, "reflex_result")
                state.add_event(
                    "reflex_recovery",
                    f"Reflex Recovery restored {reflex_out['critical_recovered_mbps']:.1f} Mbps "
                    f"critical traffic ({reflex_out['critical_guaranteed_mbps']:.1f} Mbps from verified "
                    f"guarantees) in {reflex_ms:.2f} ms of computation.",
                    {
                        "link_id": link_id,
                        "scenario": scenario,
                        "critical_recovered_mbps": reflex_out["critical_recovered_mbps"],
                        "critical_guaranteed_mbps": reflex_out["critical_guaranteed_mbps"],
                        "normal_recovered_mbps": reflex_out["normal_recovered_mbps"],
                        "runtime_ms": reflex_ms,
                    }
                )

        if not reflex_used:
            # Uncovered failure: immediate PACAR fallback (Algorithm E)
            if trace is not None:
                trace.set_phase("pacar")
                trace.emit("engine.pacar_fallback",
                           reason=("no_plan" if plan is None else
                                   ("unverified" if not plan.verified else
                                    ("not_covered" if scenario is None else "reflex_failed"))))
            pacar_results = run_pacar(state, trace=trace)
            apply_routing_results(state, pacar_results)
            if trace is not None:
                _trace_installed(state, trace, "pacar_fallback_result")
            reason = ("no verified protection plan" if plan is None or not plan.verified
                      else "failure set not covered by the single-failure plan")
            state.add_event(
                "uncovered_failure_fallback",
                f"Link {link_id} failure handled by PACAR fallback ({reason}).",
                {"link_id": link_id, "reason": reason},
            )

        # 2. Reactive LP Optimization (Algorithm D) to further optimize network
        computed_version = state.version
        if trace is not None:
            trace.set_phase("reactive_lp")
            trace.emit("engine.lp_floors",
                       floors=(plan.per_flow_guaranteed if reflex_used else None),
                       plan_exists=plan is not None, plan_verified=bool(plan is not None and plan.verified),
                       covered=bool(reflex_used), context="post_failure")
        lp_start = time.perf_counter()
        lp_out = solve_reactive_multicommodity_lp(
            demands=state.demands,
            links=state.links,
            candidate_tunnels=state.candidate_tunnels,
            previous_allocations=state.active_allocations,
            protection_floors=plan.per_flow_guaranteed if reflex_used else None,
            trace=trace,
        )
        lp_ms = (time.perf_counter() - lp_start) * 1000

        if lp_out["success"]:
            # Evaluate with Install Policy (Algorithm H)
            if trace is not None:
                trace.set_phase("install_policy")
            decision = evaluate_install_policy(
                candidate_allocation=lp_out["allocations"],
                current_allocation=state.active_allocations,
                demands=state.demands,
                links=state.links,
                candidate_tunnels=state.candidate_tunnels,
                expected_version=computed_version,
                current_version=state.version,
                trigger_reason="link_failure",
                trace=trace,
            )
            if decision.should_install:
                apply_allocation_dict(state, lp_out["allocations"], state.candidate_tunnels)
                if trace is not None:
                    _trace_installed(state, trace, "reactive_lp_result")
            state.add_event(
                "reactive_lp_optimization" if decision.should_install else "reactive_lp_not_installed",
                f"Reactive LP ({lp_ms:.2f} ms): {decision.reason}",
                {"runtime_ms": round(lp_ms, 2), "decision": decision.decision_type,
                 "installed": decision.should_install},
            )
        elif trace is not None:
            trace.emit("engine.lp_failed", solver_status=lp_out.get("solver_status"))

        if trace is not None:
            trace.set_phase("guarantee_enforcement")
        enforce_plan_guarantees(state, trace=trace)
        route_changes = state.count_route_changes()
        state.route_changes += route_changes
        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)
        version = state.bump_version()
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.failure_done", link_id=link_id, step=step, reflex_used=reflex_used,
                       reflex_time_ms=reflex_ms, scenario=scenario if reflex_used else None,
                       route_changes=route_changes, violations=violations,
                       congestion=[w["link_id"] for w in warnings])

        return {
            "version": version,
            "step": step,
            "event": "link_failure",
            "changed": True,
            "link_id": link_id,
            "reflex_used": reflex_used,
            "reflex_time_ms": round(reflex_ms, 2),
            "protection_scenario": scenario if reflex_used else None,
            "route_changes": route_changes,
            "violations": violations,
            "congestion_warnings": warnings,
        }


def handle_link_restore(state: SimulationState, link_id: str, trace=None) -> dict:
    """Restore a link, re-optimize traffic, and update protection guarantees."""
    with state.lock():
        if link_id not in state.links:
            raise ValueError(f"Link '{link_id}' not found")
        current = state.links[link_id]
        if current.status == LinkStatus.ACTIVE:
            if trace is not None:
                trace.emit("engine.noop", action="link_restore", link_id=link_id, reason="already_active")
            return _noop_result(state, link_id, "link_already_active",
                                f"Link {link_id} is already active; nothing changed.")
        if current.status == LinkStatus.FLAPPING:
            remaining = state.flap_cooldown_remaining(link_id)
            if trace is not None:
                trace.emit("engine.noop", action="link_restore", link_id=link_id, reason="held_down",
                           remaining=remaining)
            return _noop_result(state, link_id, "link_held_down",
                                f"Link {link_id} is held down by flap damping; it returns to service "
                                f"automatically after {remaining} more controller step(s).")
        step = state.bump_step()
        link = state.restore_link(link_id)
        event_type = "link_restore"
        msg = f"Link {link.source}↔{link.target} ({link_id}) restored."
        if link.status == LinkStatus.FLAPPING:
            event_type = "link_flapping"
            msg = (
                f"Link {link.source}↔{link.target} ({link_id}) failed {link.flap_count} times within "
                f"{state.flap_window} steps and is held down (flap damping) for {state.flap_cooldown} "
                "steps instead of returning to service."
            )
        state.add_event(event_type, msg, {
            "link_id": link_id,
            "status": link.status.value,
            "flap_count": link.flap_count,
        })
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.restore_start", link_id=link_id, step=step, status=link.status.value,
                       flap_count=link.flap_count, source=link.source, target=link.target,
                       held_down=link.status == LinkStatus.FLAPPING,
                       allocations_before=state.active_allocations,
                       loads_before={lid: l.allocated_load for lid, l in state.links.items()})

        # Refresh candidate tunnels
        refresh_candidate_tunnels(state, trace=trace)
        state.save_routes_snapshot()

        # Plan protection for the restored (or still held-down) network first: the
        # plan depends only on topology, demands and candidate paths, and its
        # verified guarantees then act as floors for the re-optimization.
        update_simulation_protection_plan(state)
        if trace is not None:
            _trace_plan_summary(state, trace)
        plan = state.protection_plan
        floors = None
        if plan is not None and plan.verified and plan.scenario_for(unavailable_links(state)) is not None:
            floors = {fid: g for fid, g in plan.per_flow_guaranteed.items() if g > 1e-9}

        # Run Reactive LP Optimizer
        if trace is not None:
            trace.set_phase("reactive_lp")
            trace.emit("engine.lp_floors", floors=floors, plan_exists=plan is not None,
                       plan_verified=bool(plan is not None and plan.verified),
                       covered=floors is not None, context="restore")
        lp_out = solve_reactive_multicommodity_lp(
            demands=state.demands,
            links=state.links,
            candidate_tunnels=state.candidate_tunnels,
            previous_allocations=state.active_allocations,
            protection_floors=floors,
            trace=trace,
        )
        if lp_out["success"]:
            apply_allocation_dict(state, lp_out["allocations"], state.candidate_tunnels)
            if trace is not None:
                _trace_installed(state, trace, "reactive_lp_result")
        else:
            if trace is not None:
                trace.set_phase("pacar")
                trace.emit("engine.lp_fallback", solver_status=lp_out.get("solver_status"))
            results = run_pacar(state, trace=trace)
            apply_routing_results(state, results)
            if trace is not None:
                _trace_installed(state, trace, "pacar_result")
        if trace is not None:
            trace.set_phase("guarantee_enforcement")
        enforce_plan_guarantees(state, trace=trace)

        route_changes = state.count_route_changes()
        state.route_changes += route_changes
        violations = validate_allocation(state)
        warnings = get_congestion_warnings(state)
        version = state.bump_version()
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.restore_done", link_id=link_id, step=step, status=link.status.value,
                       route_changes=route_changes, violations=violations,
                       congestion=[w["link_id"] for w in warnings])

        return {
            "version": version,
            "step": step,
            "event": event_type,
            "changed": True,
            "link_id": link_id,
            "status": link.status.value,
            "cooldown_remaining": state.flap_cooldown_remaining(link_id),
            "route_changes": route_changes,
            "violations": violations,
            "congestion_warnings": warnings,
        }


def handle_batch_failures(state: SimulationState, link_ids: list[str], trace=None) -> dict:
    """Fail multiple links and re-simulate once (uncovered multi-failure scenario)."""
    events = []
    with state.lock():
        unknown = sorted(set(link_ids) - set(state.links))
        if unknown:
            raise ValueError(f"Unknown link IDs: {', '.join(unknown)}")
        step = state.bump_step()
        for link_id in dict.fromkeys(link_ids):
            if state.links[link_id].status == LinkStatus.FAILED:
                continue
            link = state.fail_link(link_id)
            state.add_event(
                "link_failure",
                f"Link {link.source}↔{link.target} ({link_id}) failed (batch).",
                {"link_id": link_id, "batch": True},
            )
            events.append(link_id)
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.batch_failed", requested=list(link_ids), failed=list(events), step=step)

        # Re-simulate with PACAR at the same controller step
        result = _simulate_at_step(state, "pacar", step, trace=trace)
    result["event"] = "batch_failure"
    result["failed_links"] = events
    return result


def add_emergency_demand(state: SimulationState, demand: DemandCreate, trace=None) -> dict:
    """Add an emergency demand (P1) and re-simulate with preemption."""
    with state.lock():
        d = Demand(
            id=demand.id,
            source=demand.source,
            destination=demand.destination,
            bandwidth=demand.bandwidth,
            category=TrafficCategory.EMERGENCY,
            priority=Priority.P1_EMERGENCY,
            max_latency=demand.max_latency,
        )
        state.add_demand(d)
        step = state.bump_step()
        state.add_event(
            "emergency_demand",
            f"Emergency demand {demand.id} added: {demand.source}→{demand.destination} "
            f"({demand.bandwidth} Mbps, emergency).",
            {"demand_id": demand.id, "bandwidth": demand.bandwidth},
        )
        if trace is not None:
            trace.set_phase("controller")
            trace.emit("engine.emergency_added", demand_id=demand.id, source=demand.source,
                       destination=demand.destination, bandwidth=demand.bandwidth, step=step)
        result = _simulate_at_step(state, "pacar", step, trace=trace)
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
    Each snapshot regenerates its candidate tunnels from the current topology, so
    every algorithm sees exactly the same network (including current failures).
    """
    from app.analysis.metrics import compute_metrics

    # 1. Snapshot for PACAR
    snap_pacar = state.snapshot()
    refresh_candidate_tunnels(snap_pacar)
    start = time.perf_counter()
    pacar_results = run_pacar(snap_pacar)
    pacar_time = (time.perf_counter() - start) * 1000
    apply_routing_results(snap_pacar, pacar_results)
    pacar_metrics = compute_metrics(snap_pacar)
    pacar_metrics["computation_time_ms"] = round(pacar_time, 2)
    pacar_metrics["algorithm"] = "PACAR"
    pacar_metrics["optimality"] = "heuristic (no optimality guarantee)"

    # 2. Snapshot for Reactive LP
    snap_lp = state.snapshot()
    refresh_candidate_tunnels(snap_lp)
    start_lp = time.perf_counter()
    lp_res = solve_reactive_multicommodity_lp(snap_lp.demands, snap_lp.links, snap_lp.candidate_tunnels)
    lp_time = (time.perf_counter() - start_lp) * 1000
    lp_fallback = not (lp_res["success"] and lp_res["allocations"])
    if not lp_fallback:
        apply_allocation_dict(snap_lp, lp_res["allocations"], snap_lp.candidate_tunnels)
    else:
        apply_routing_results(snap_lp, run_pacar(snap_lp))
    lp_metrics = compute_metrics(snap_lp)
    lp_metrics["computation_time_ms"] = round(lp_time, 2)
    lp_metrics["algorithm"] = "Reactive Multicommodity LP" + (" (failed; PACAR fallback shown)" if lp_fallback else "")
    lp_metrics["fallback_used"] = lp_fallback
    lp_metrics["optimality"] = ("lexicographic optimum over candidate tunnels"
                                if not lp_fallback else f"solver failed: {lp_res.get('solver_status')}")

    # 3. Snapshot for Baseline (Dijkstra)
    snap_baseline = state.snapshot()
    refresh_candidate_tunnels(snap_baseline)
    start_base = time.perf_counter()
    baseline_results = run_baseline(snap_baseline)
    baseline_time = (time.perf_counter() - start_base) * 1000
    apply_routing_results(snap_baseline, baseline_results)
    baseline_metrics = compute_metrics(snap_baseline)
    baseline_metrics["computation_time_ms"] = round(baseline_time, 2)
    baseline_metrics["algorithm"] = "Baseline (Dijkstra)"
    baseline_metrics["optimality"] = "heuristic baseline"

    return {
        "pacar": pacar_metrics,
        "reactive_lp": lp_metrics,
        "baseline": baseline_metrics,
    }
