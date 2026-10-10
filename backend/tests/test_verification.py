"""Independent numerical checks and regressions for the local QA audit."""
from types import SimpleNamespace

import networkx as nx
import pytest

from app.models import Node, Link, Demand, TrafficCategory, LinkStatus
from app.state import SimulationState
from app.routing.candidate_paths import CandidateTunnel, generate_all_candidate_tunnels, build_candidate_graph
from app.routing.protection_planner import plan_proactive_protection
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.reflex import run_reflex_recovery
from app.routing.reactive_lp import solve_reactive_multicommodity_lp
from app.routing.pacar import run_pacar, run_pacar_allocations, find_k_shortest_paths
from app.routing.validator import validate_allocation_independently
from app.routing.install_policy import evaluate_install_policy
from app.routing.allocation import apply_allocation_dict, apply_routing_results
from app.analysis.metrics import compute_metrics
from app.simulation.engine import simulate, handle_link_failure


def diamond(critical=6, normal=12):
    s = SimulationState()
    for n in 'ABCD':
        s.add_node(Node(id=n, name=n))
    for u, v in [('A', 'B'), ('B', 'D'), ('A', 'C'), ('C', 'D')]:
        s.add_link(Link(id=u+v, source=u, target=v, capacity=10, latency=1))
    s.add_demand(Demand(id='F1', source='A', destination='D', bandwidth=critical,
                        category=TrafficCategory.EMERGENCY))
    s.add_demand(Demand(id='F2', source='A', destination='D', bandwidth=normal))
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    return s


def flattened(s):
    return {t.id: t for ts in s.candidate_tunnels.values() for t in ts}


def validate(s, alloc, **kw):
    return validate_allocation_independently(alloc, s.demands, s.links, flattened(s), **kw)


def verified_plan(s):
    p = plan_proactive_protection(s.demands, s.links, s.candidate_tunnels)
    assert verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels).is_verified
    return p


def test_canonical_example(monkeypatch):
    s = diamond()
    p = verified_plan(s)
    assert p.per_flow_guaranteed['F1'] == pytest.approx(6, abs=1e-8)
    assert sorted(p.per_tunnel_reservations['F1'].values()) == pytest.approx([6, 6], abs=1e-8)
    initial = {'F1': {'T_F1_1': 3, 'T_F1_2': 3}, 'F2': {'T_F2_1': 6, 'T_F2_2': 6}}
    assert validate(s, initial).is_valid
    def forbidden(*a, **k):
        raise AssertionError('Reflex must not call a solver')
    monkeypatch.setattr('scipy.optimize.linprog', forbidden)
    monkeypatch.setattr('app.routing.protection_planner.linprog', forbidden)
    monkeypatch.setattr('app.routing.reactive_lp.linprog', forbidden)
    out = run_reflex_recovery({'BD'}, p, s.demands, s.links, s.candidate_tunnels, initial)
    assert out['success']
    assert out['allocations'] == {'F1': {'T_F1_2': pytest.approx(6)}, 'F2': {'T_F2_2': pytest.approx(4)}}
    val = validate(s, out['allocations'], failed_links={'BD'})
    assert val.is_valid, val.violations
    assert val.link_loads['CD'] == pytest.approx(10)
    s.fail_link('BD')
    apply_allocation_dict(s, out['allocations'], s.candidate_tunnels)
    assert compute_metrics(s)['critical_delivered'] == 6


@pytest.mark.parametrize('rate', [float('inf'), float('-inf'), float('nan'), -1, None])
def test_validator_rejects_nonfinite_and_negative(rate):
    s = diamond()
    assert not validate(s, {'F1': {'T_F1_1': rate}}).is_valid


@pytest.mark.parametrize('corruption', ['source', 'destination', 'disconnected', 'mapping', 'latency', 'ownership', 'missing_link', 'repeated', 'empty'])
def test_validator_reconstructs_path(corruption):
    s = diamond()
    t = s.candidate_tunnels['F1'][0]
    if corruption == 'source': t.node_path[0] = 'C'
    if corruption == 'destination': t.node_path[-1] = 'C'
    if corruption == 'disconnected': t.node_path = ['A', 'D']
    if corruption == 'mapping': t.link_ids = ['AC', 'CD']
    if corruption == 'latency':
        s.demands['F1'] = s.demands['F1'].model_copy(update={'max_latency': 1})
        t.latency = 0
    if corruption == 'ownership': t.flow_id = 'F2'
    if corruption == 'missing_link': t.link_ids[0] = 'missing'
    if corruption == 'repeated': t.node_path = ['A', 'B', 'A', 'D']
    if corruption == 'empty': t.node_path = []; t.link_ids = []
    assert not validate(s, {'F1': {t.id: 1}}).is_valid


@pytest.mark.parametrize('corruption', ['empty_scenarios', 'unknown_scenario', 'nan_guarantee', 'negative_guarantee', 'overreservation', 'unknown_tunnel', 'negative_reservation', 'nan_reservation', 'excess_guarantee'])
def test_checker_rejects_invalid_plan(corruption):
    s = diamond()
    p = verified_plan(s)
    if corruption == 'empty_scenarios': p.covered_scenarios = []
    if corruption == 'unknown_scenario': p.covered_scenarios = ['fail_unknown']
    if corruption == 'nan_guarantee': p.per_flow_guaranteed['F1'] = float('nan')
    if corruption == 'negative_guarantee': p.per_flow_guaranteed['F1'] = -1
    if corruption == 'overreservation': p.per_tunnel_reservations['F1']['T_F1_1'] = 20
    if corruption == 'unknown_tunnel': p.per_tunnel_reservations['F1']['fake'] = 1
    if corruption == 'negative_reservation': p.per_tunnel_reservations['F1']['T_F1_1'] = -1
    if corruption == 'nan_reservation': p.per_tunnel_reservations['F1']['T_F1_1'] = float('nan')
    if corruption == 'excess_guarantee': p.per_flow_guaranteed['F1'] = 7
    assert not verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels).is_verified
    assert not p.verified


def test_solver_failure_is_not_success(monkeypatch):
    s = diamond()
    monkeypatch.setattr('app.routing.reactive_lp.linprog', lambda *a, **kw: SimpleNamespace(success=False, message='injected failure'))
    out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)
    assert not out['success']
    assert out['solver_status'] != 'optimal'


@pytest.mark.parametrize('algorithm', ['lp', 'pacar', 'planner'])
def test_algorithms_reject_latency_ineligible_tunnels(algorithm):
    s = diamond()
    s.demands['F1'] = s.demands['F1'].model_copy(update={'max_latency': 1})
    if algorithm == 'lp': alloc = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)['allocations']
    elif algorithm == 'pacar': alloc = run_pacar_allocations(s.demands, s.links, s.candidate_tunnels)
    else:
        p = plan_proactive_protection(s.demands, s.links, s.candidate_tunnels)
        assert p.per_flow_guaranteed['F1'] == 0
        return
    assert sum(alloc['F1'].values()) == 0


@pytest.mark.parametrize('trigger', ['normal', 'link_failure'])
@pytest.mark.parametrize('offset', [-1, 1])
def test_install_rejects_any_version_mismatch(trigger, offset):
    s = diamond()
    d = evaluate_install_policy({'F1': {'T_F1_1': 6}}, {}, s.demands, s.links, s.candidate_tunnels,
                                10+offset, 10, trigger_reason=trigger)
    assert not d.should_install
    assert d.decision_type == 'rejected_stale'


def test_multiple_sequential_failures_use_all_failed_links():
    s = diamond()
    simulate(s, 'lp')
    assert handle_link_failure(s, 'BD')['reflex_used']
    out = handle_link_failure(s, 'AC')
    assert not out['reflex_used']
    assert s.demands['F1'].allocated_bandwidth == 0
    assert not out['violations']


def test_single_path_install_clears_old_multipath_state():
    s = diamond()
    apply_allocation_dict(s, {'F1': {'T_F1_2': 6}}, s.candidate_tunnels)
    apply_routing_results(s, run_pacar(s))
    assert sum(s.active_allocations['F1'].values()) == s.demands['F1'].allocated_bandwidth
    assert s.demands['F1'].tunnels[0]['node_path'] == s.demands['F1'].route


def test_failed_configured_tunnel_is_not_delivered():
    s = diamond()
    apply_allocation_dict(s, {'F1': {'T_F1_1': 3, 'T_F1_2': 3}, 'F2': {}}, s.candidate_tunnels)
    s.fail_link('BD')
    assert compute_metrics(s)['critical_delivered'] == 3


def test_allocation_install_is_atomic_on_invalid_input():
    s = diamond()
    before = s.snapshot()
    with pytest.raises(ValueError):
        apply_allocation_dict(s, {'F1': {'fake': 3}}, s.candidate_tunnels)
    assert s.demands == before.demands
    assert s.links == before.links
    assert s.version == before.version


def test_snapshot_copies_routing_state():
    s = diamond()
    s.protection_plan = verified_plan(s)
    s.active_allocations = {'F1': {'T_F1_1': 3}}
    snap = s.snapshot()
    assert snap.active_allocations == s.active_allocations
    snap.active_allocations['F1']['T_F1_1'] = 1
    assert s.active_allocations['F1']['T_F1_1'] == 3
    assert snap.protection_plan == s.protection_plan


def test_zero_capacity_candidate_graph():
    s = diamond()
    s.links['AB'] = s.links['AB'].model_copy(update={'capacity': 0, 'latency': 0})
    graph, _ = build_candidate_graph(s.nodes, s.links)
    assert graph['A']['B']['capacity'] == 0
    assert graph['A']['B']['latency'] == 0


def test_k_shortest_paths_does_not_exhaust_generator(monkeypatch):
    calls = []
    def paths(*a, **kw):
        for i in range(100):
            calls.append(i)
            yield ['A', str(i), 'D']
    monkeypatch.setattr(nx, 'shortest_simple_paths', paths)
    assert len(find_k_shortest_paths(nx.Graph(), 'A', 'D', 5)) == 5
    assert len(calls) == 5


def test_pacar_heuristic_is_less_optimal_than_lp():
    s = diamond(critical=15, normal=1)
    s.remove_demand('F2')
    heuristic = run_pacar(s)[0].allocated_bandwidth
    optimum = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)['total_delivered']
    assert heuristic == 10
    assert optimum == pytest.approx(15, abs=1e-4)


@pytest.mark.parametrize('seed', range(10))
def test_randomized_capacity_demand_and_single_failure_guarantees(seed):
    import random
    rng = random.Random(seed)
    s = diamond(rng.uniform(1, 18), rng.uniform(1, 25))
    for lid in s.links:
        s.links[lid] = s.links[lid].model_copy(update={'capacity': rng.uniform(1, 15)})
    p = verified_plan(s)
    known = min(s.demands['F1'].bandwidth, *(l.capacity for l in s.links.values()))
    assert p.per_flow_guaranteed['F1'] == pytest.approx(known, abs=1e-4)
    for failure in [set(), *[{lid} for lid in s.links]]:
        out = run_reflex_recovery(failure, p, s.demands, s.links, s.candidate_tunnels)
        assert validate(s, out['allocations'], failed_links=failure).is_valid
        assert sum(out['allocations']['F1'].values()) >= known - 1e-4
    for algorithm in (solve_reactive_multicommodity_lp, run_pacar_allocations):
        out = algorithm(s.demands, s.links, s.candidate_tunnels)
        alloc = out['allocations'] if algorithm is solve_reactive_multicommodity_lp else out
        assert validate(s, alloc).is_valid


def test_canonical_loss_and_per_flow_clamping():
    from app.analysis.metrics import integrate_critical_loss
    delay = .20 + .01 + .05
    assert delay == pytest.approx(.26)
    assert integrate_critical_loss([(delay, {'F1': 6}, {'F1': 3})]) == pytest.approx(.78)
    assert integrate_critical_loss([(1, {'F1': 6, 'F3': 2}, {'F1': 8, 'F3': 0})]) == 2
    assert integrate_critical_loss([(.1, {'F1': 6}, {'F1': 0}), (.2, {'F1': 6}, {'F1': 3})]) == pytest.approx(1.2)
    with pytest.raises(ValueError):
        integrate_critical_loss([(-1, {}, {})])


@pytest.mark.parametrize('fault', ['failed', 'exception'])
def test_planner_solver_failure_is_unverified(monkeypatch, fault):
    s = diamond()
    def broken(*a, **kw):
        if fault == 'exception': raise RuntimeError('solver unavailable')
        return SimpleNamespace(success=False, message='injected infeasible')
    monkeypatch.setattr('app.routing.protection_planner.linprog', broken)
    p = plan_proactive_protection(s.demands, s.links, s.candidate_tunnels)
    assert not p.verified
    assert not verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels).is_verified


def test_optimizer_preserves_per_flow_floors():
    s = diamond(6, 1)
    s.add_demand(Demand(id='F3', source='A', destination='D', bandwidth=20, category=TrafficCategory.EMERGENCY))
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    s.fail_link('BD')
    out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels, protection_floors={'F1': 4, 'F3': 4})
    assert out['success']
    assert validate(s, out['allocations']).is_valid
    assert sum(out['allocations']['F1'].values()) >= 4-1e-8
    assert sum(out['allocations']['F3'].values()) >= 4-1e-8
    assert sum(sum(out['allocations'][fid].values()) for fid in ('F1', 'F3')) == pytest.approx(10)
    infeasible = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels, protection_floors={'F1': 6, 'F3': 6})
    assert not infeasible['success']


@pytest.mark.parametrize('edge_case', ['zero_demand', 'disconnected', 'zero_capacity', 'mapping'])
def test_numerical_degenerate_cases(edge_case):
    s = diamond()
    if edge_case == 'zero_demand': s.demands['F1'] = s.demands['F1'].model_copy(update={'bandwidth': 0})
    if edge_case == 'disconnected': s.candidate_tunnels['F1'] = []
    if edge_case == 'zero_capacity':
        for lid in s.links: s.links[lid] = s.links[lid].model_copy(update={'capacity': 0})
    if edge_case == 'mapping':
        for t in s.candidate_tunnels['F1']: t.link_ids = ['AB']
    p = plan_proactive_protection(s.demands, s.links, s.candidate_tunnels)
    assert p.per_flow_guaranteed['F1'] == 0
    for algorithm in (solve_reactive_multicommodity_lp, run_pacar_allocations):
        out = algorithm(s.demands, s.links, s.candidate_tunnels)
        alloc = out['allocations'] if algorithm is solve_reactive_multicommodity_lp else out
        assert not alloc['F1']
        assert validate(s, alloc).is_valid


def test_reflex_caps_preserved_normal_traffic_at_changed_demand():
    s = diamond()
    p = verified_plan(s)
    s.demands['F2'] = s.demands['F2'].model_copy(update={'bandwidth': 2})
    out = run_reflex_recovery({'BD'}, p, s.demands, s.links, s.candidate_tunnels, {'F2': {'T_F2_2': 12}})
    assert out['success']
    assert sum(out['allocations']['F2'].values()) == 2


def test_pacar_enforces_latency_on_live_state():
    s = diamond()
    s.demands['F1'] = s.demands['F1'].model_copy(update={'max_latency': 1})
    result = {d.id: d for d in run_pacar(s)}
    assert result['F1'].allocated_bandwidth == 0


def test_stale_demand_change_invalidates_plan():
    s = diamond()
    simulate(s)
    assert s.protection_plan.verified
    s.update_demand('F1', bandwidth=7)
    assert s.protection_plan is None
    assert not handle_link_failure(s, 'BD')['reflex_used']


def test_replan_api_does_not_deadlock():
    import concurrent.futures
    from fastapi.testclient import TestClient
    from app.main import app
    with concurrent.futures.ThreadPoolExecutor() as executor:
        result = executor.submit(lambda: TestClient(app).post('/protection/replan')).result(timeout=5)
        assert result.status_code == 200


def test_validator_rejects_unknown_identifiers_even_at_zero_rate():
    s = diamond()
    assert not validate(s, {'unknown': {}}).is_valid
    assert not validate(s, {'F1': {'unknown': 0}}).is_valid


def test_reflex_reports_uncovered_and_unverified_plans():
    s = diamond()
    p = verified_plan(s)
    assert not run_reflex_recovery({'AB', 'AC'}, p, s.demands, s.links, s.candidate_tunnels)['success']
    p.verified = False
    assert not run_reflex_recovery({'AB'}, p, s.demands, s.links, s.candidate_tunnels)['success']


def test_reflex_returns_computation_network_version():
    s = diamond()
    p = verified_plan(s)
    out = run_reflex_recovery({'BD'}, p, s.demands, s.links, s.candidate_tunnels, network_version=123)
    assert out['network_version'] == 123


def test_stale_atomic_install_does_not_mutate():
    s = diamond()
    before = s.version
    with pytest.raises(ValueError, match='Stale'):
        apply_allocation_dict(s, {'F1': {'T_F1_1': 6}}, s.candidate_tunnels, expected_version=before-1)
    assert s.version == before
    assert not s.active_allocations


def test_pacar_equal_priority_progressive_filling():
    s = diamond(1, 12)
    s.remove_demand('F1')
    s.add_demand(Demand(id='F3', source='A', destination='D', bandwidth=12))
    out = {d.id: d.allocated_bandwidth for d in run_pacar(s)}
    assert out == {'F2': 5, 'F3': 5}


def test_lp_latency_minimization_preserves_throughput():
    s = diamond(6, 1)
    s.remove_demand('F2')
    s.links['AC'] = s.links['AC'].model_copy(update={'latency': 10})
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)
    assert out['allocations']['F1'] == {'T_F1_1': pytest.approx(6, abs=1e-8)}


def test_overlapping_reservations_obey_shared_edges():
    s = diamond(8, 1)
    s.remove_demand('F2')
    s.add_demand(Demand(id='F3', source='A', destination='D', bandwidth=8, category=TrafficCategory.EMERGENCY))
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    p = verified_plan(s)
    assert sum(p.per_flow_guaranteed.values()) == pytest.approx(10, abs=1e-8)
    for lid in s.links:
        load = sum(rate for fid, alloc in p.per_tunnel_reservations.items()
                   for tid, rate in alloc.items() if lid in flattened(s)[tid].link_ids)
        assert load <= 10+1e-8


def test_version_does_not_rewind_on_reset():
    s = diamond()
    old = s.version
    s.reset()
    assert s.version > old


def test_failure_during_optimizer_rejects_stale_candidate(monkeypatch):
    s = diamond()
    simulate(s, 'lp')
    solver = solve_reactive_multicommodity_lp
    def interrupted(*args, **kwargs):
        out = solver(*args, **kwargs)
        s.fail_link('AC')
        return out
    monkeypatch.setattr('app.simulation.engine.solve_reactive_multicommodity_lp', interrupted)
    handle_link_failure(s, 'BD')
    assert not any(e.event_type == 'reactive_lp_optimization' for e in s.events)


def test_solver_failure_keeps_valid_pacar_fallback(monkeypatch):
    s = diamond()
    monkeypatch.setattr('app.simulation.engine.solve_reactive_multicommodity_lp',
                        lambda *a, **k: {'success': False, 'allocations': {}})
    result = simulate(s, 'lp')
    assert not result['violations']
    assert validate(s, s.active_allocations).is_valid
    assert s.demands['F1'].allocated_bandwidth == 6


def test_flapping_links_are_not_used_by_lp_or_tunnel_pacar():
    s = diamond()
    s.links['BD'] = s.links['BD'].model_copy(update={'status': LinkStatus.FLAPPING})
    for algorithm in (solve_reactive_multicommodity_lp, run_pacar_allocations):
        out = algorithm(s.demands, s.links, s.candidate_tunnels)
        alloc = out['allocations'] if algorithm is solve_reactive_multicommodity_lp else out
        assert validate(s, alloc).is_valid
        assert 'T_F1_1' not in alloc['F1']


def test_omitted_flow_is_cleared_by_atomic_install():
    s = diamond()
    apply_allocation_dict(s, {'F1': {'T_F1_1': 6}, 'F2': {'T_F2_2': 8}}, s.candidate_tunnels)
    apply_allocation_dict(s, {'F1': {'T_F1_1': 6}}, s.candidate_tunnels)
    assert s.demands['F2'].allocated_bandwidth == 0
    assert s.active_allocations['F2'] == {}


@pytest.mark.parametrize('seed', range(10))
def test_random_small_topologies_and_reflex_preemption(seed):
    import random
    rng = random.Random(seed+100)
    s = SimulationState()
    for n in 'ABCDE': s.add_node(Node(id=n, name=n))
    pairs = [(u, v) for i, u in enumerate('ABCDE') for v in 'ABCDE'[i+1:]]
    for u, v in rng.sample(pairs, 7):
        s.add_link(Link(id=u+v, source=u, target=v, capacity=rng.uniform(2, 20), latency=rng.uniform(.1, 4)))
    for i in range(4):
        u, v = rng.sample(list('ABCDE'), 2)
        s.add_demand(Demand(id=f'F{i}', source=u, destination=v, bandwidth=rng.uniform(1, 15),
            category=TrafficCategory.EMERGENCY if i < 2 else TrafficCategory.GENERAL))
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    p = verified_plan(s)
    lp = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels,
                                          protection_floors=p.per_flow_guaranteed)
    assert lp['success']
    assert validate(s, lp['allocations']).is_valid
    for lid in s.links:
        recovered = run_reflex_recovery({lid}, p, s.demands, s.links, s.candidate_tunnels, lp['allocations'])
        assert recovered['success'], recovered
        checked = validate(s, recovered['allocations'], failed_links={lid})
        assert checked.is_valid, checked.violations
        for fid, guaranteed in p.per_flow_guaranteed.items():
            assert sum(recovered['allocations'][fid].values()) >= guaranteed-1e-8


def test_baseline_does_not_deliver_latency_ineligible_flow():
    from app.routing.baseline import run_baseline
    s = diamond()
    s.demands['F1'] = s.demands['F1'].model_copy(update={'max_latency': 1})
    results = {d.id: d for d in run_baseline(s)}
    assert results['F1'].allocated_bandwidth == 0


def test_local_flows_work_with_tunnel_algorithms():
    s = SimulationState()
    s.add_node(Node(id='A', name='A'))
    s.add_demand(Demand(id='F1', source='A', destination='A', bandwidth=6, category=TrafficCategory.EMERGENCY))
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)
    assert out['total_delivered'] == 6
    assert sum(run_pacar_allocations(s.demands, s.links, s.candidate_tunnels)['F1'].values()) == 6
    p = verified_plan(s)
    assert p.per_flow_guaranteed['F1'] == 6
