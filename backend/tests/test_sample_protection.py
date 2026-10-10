"""Prove sample guarantees using independent graph cuts and exhaustive paths."""
import networkx as nx
import pytest

from app.models import Demand, TrafficCategory
from app.state import SimulationState
from app.scenarios.presets import load_scenario
from app.simulation.engine import simulate, handle_link_failure
from app.routing.candidate_paths import CandidateTunnel, build_candidate_graph
from app.routing.protection_planner import plan_proactive_protection
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.reflex import run_reflex_recovery
from app.routing.validator import validate_allocation_independently


def load(preset):
    data = load_scenario(preset)
    state = SimulationState()
    state.load_topology(data['nodes'], data['links'], data['demands'])
    simulate(state)
    return state


@pytest.mark.parametrize('fid,cuts', [
    ('D1', {'L8', 'L9'}), ('D4', {'L4', 'L8'}),
    ('D6', {'L7', 'L11'}), ('D7', {'L4', 'L7', 'L9', 'L11'}),
])
def test_critical_zero_guarantee_forced_by_real_disconnect(fid, cuts):
    s = load('normal_campus')
    graph, _ = build_candidate_graph(s.nodes, s.links)
    d = s.demands[fid]
    disconnects = set()
    for lid, link in s.links.items():
        failed = graph.copy()
        failed.remove_edge(link.source, link.target)
        if not nx.has_path(failed, d.source, d.destination):
            disconnects.add(lid)
    assert disconnects == cuts
    for lid in cuts:
        assert 'fail_' + lid in s.protection_plan.covered_scenarios
        assert all(lid in t.link_ids for t in s.candidate_tunnels[fid])
    assert s.protection_plan.per_flow_guaranteed[fid] == 0


def test_default_sample_complete_candidate_set_still_has_zero_optimum():
    s = load('normal_campus')
    assert (len(s.nodes), len(s.links), len(s.demands)) == (10, 11, 8)
    graph, edge_map = build_candidate_graph(s.nodes, s.links)
    complete = {}
    for fid, d in s.demands.items():
        paths = list(nx.all_simple_paths(graph, d.source, d.destination))
        complete[fid] = [CandidateTunnel(f'exhaustive_{fid}_{i}', fid, path,
            [edge_map[u, v] for u, v in zip(path, path[1:])],
            sum(graph[u][v]['latency'] for u, v in zip(path, path[1:])))
            for i, path in enumerate(paths)]
        if d.priority.value in (1, 2):
            assert {tuple(t.node_path) for t in s.candidate_tunnels[fid]} == {tuple(path) for path in paths}
    p = plan_proactive_protection(s.demands, s.links, complete)
    assert p.solver_status == 'optimal'
    assert p.covered_scenarios == ['no_failure', *['fail_' + lid for lid in s.links]]
    assert len(p.covered_scenarios) == 12
    assert set(p.per_flow_guaranteed) == set(s.demands)
    assert all(g == 0 for g in p.per_flow_guaranteed.values())
    assert all(not reservations for reservations in p.per_tunnel_reservations.values())
    checked = verify_protection_guarantees(p, s.demands, s.links, complete)
    assert checked.is_verified and checked.scenarios_passed == 12


def test_zero_plan_verification_does_not_accept_false_positive_guarantee():
    s = load('normal_campus')
    p = s.protection_plan
    p.per_flow_guaranteed['D6'] = 1
    checked = verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels)
    assert not checked.is_verified
    assert not p.verified


@pytest.mark.parametrize('with_p1', [False, True])
@pytest.mark.parametrize('failed_link', ['L1', 'L3'])
def test_positive_demo_verified_for_every_legitimate_failure(with_p1, failed_link):
    s = load('congested_shortest')
    if with_p1:
        s.add_demand(Demand(id='P1_DEMO', source='A', destination='E', bandwidth=10,
                           category=TrafficCategory.EMERGENCY))
        simulate(s)
    p = s.protection_plan
    assert p.solver_status == 'optimal' and p.verified
    assert p.per_flow_guaranteed['D1'] == 40
    assert sorted(p.per_tunnel_reservations['D1'].values()) == [40, 40]
    if with_p1:
        assert p.per_flow_guaranteed['P1_DEMO'] == 10
        assert sorted(p.per_tunnel_reservations['P1_DEMO'].values()) == [10, 10]
    assert len(p.covered_scenarios) == 6
    tunnels = {t.id: t for ts in s.candidate_tunnels.values() for t in ts}
    for lid in s.links:
        recovered = run_reflex_recovery({lid}, p, s.demands, s.links, s.candidate_tunnels,
                                        previous_allocations=s.active_allocations)
        assert recovered['success']
        valid = validate_allocation_independently(recovered['allocations'], s.demands, s.links,
                                                  tunnels, failed_links={lid})
        assert valid.is_valid, valid.violations
        assert sum(recovered['allocations']['D1'].values()) == pytest.approx(40)
        if with_p1:
            assert sum(recovered['allocations']['P1_DEMO'].values()) == pytest.approx(10)
    failure = handle_link_failure(s, failed_link)
    assert failure['reflex_used']
    assert not failure['violations']
    assert s.demands['D1'].allocated_bandwidth == pytest.approx(40)
    if with_p1:
        assert s.demands['P1_DEMO'].allocated_bandwidth == pytest.approx(10)
        if failed_link == 'L3':
            assert s.demands['D2'].allocated_bandwidth == 0
            assert s.demands['D3'].allocated_bandwidth == 0
