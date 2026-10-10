"""Final release hardening: regressions for FB-1..FB-6 and defects found by the
randomized property harness (qa/property_fuzz.py), plus hand-calculated
algorithm examples. Every API test uses an isolated state; never port 8000."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import Node, Link, Demand, TrafficCategory, LinkStatus, DemandStatus
from app.state import SimulationState
from app.routing.candidate_paths import generate_all_candidate_tunnels
from app.routing.reactive_lp import solve_reactive_multicommodity_lp
from app.routing.reflex import run_reflex_recovery
from app.routing.install_policy import evaluate_install_policy
from app.simulation import engine
from app.simulation.engine import simulate, handle_link_failure, handle_link_restore


@pytest.fixture
def api(monkeypatch):
    s = SimulationState()
    monkeypatch.setattr(main, 'state', s)
    with TestClient(main.app) as client:
        yield client, s


def protected_demo(client):
    assert client.post('/topology/generate', json={'preset': 'congested_shortest'}).status_code == 200
    assert client.post('/demands', json={'id': 'P1_DEMO', 'source': 'A', 'destination': 'E',
                                         'bandwidth': 10, 'category': 'emergency'}).status_code == 200
    assert client.post('/simulate?algorithm=pacar').json()['violations'] == []


def build(nodes, links, demands):
    s = SimulationState()
    for n in nodes:
        s.add_node(Node(id=n, name=n))
    for lid, u, v, cap, lat in links:
        s.add_link(Link(id=lid, source=u, target=v, capacity=cap, latency=lat))
    for did, u, v, bw, cat in demands:
        s.add_demand(Demand(id=did, source=u, destination=v, bandwidth=bw, category=cat))
    return s


def link_status(client, lid):
    return next(l for l in client.get('/topology').json()['links'] if l['id'] == lid)


# ── FB-1: presentation-only node edits ─────────────────────────────────

@pytest.mark.parametrize('payload', [{'x': 321, 'y': 12}, {'name': 'Renamed'}, {'node_type': 'server'}])
def test_fb1_node_presentation_edit_keeps_plan_and_reflex(api, payload):
    client, s = api
    protected_demo(client)
    plan_id = s.protection_plan.plan_id
    assert client.put('/nodes/A', json=payload).status_code == 200
    assert s.protection_plan is not None and s.protection_plan.plan_id == plan_id
    assert client.post('/links/L3/fail').json()['reflex_used'] is True


# ── FB-2: flap damping counts failures ─────────────────────────────────

def test_fb2_two_cycles_stay_active_third_failure_holds_down(api):
    client, s = api
    protected_demo(client)
    for _ in range(2):
        assert client.post('/links/L3/fail').json()['event'] == 'link_failure'
        assert client.post('/links/L3/restore').json()['event'] == 'link_restore'
    assert link_status(client, 'L3')['status'] == 'active'
    assert link_status(client, 'L3')['flap_count'] == 2
    assert client.post('/links/L3/fail').json()['reflex_used'] is True
    r = client.post('/links/L3/restore').json()
    assert r['event'] == 'link_flapping' and r['status'] == 'flapping' and r['cooldown_remaining'] == 5
    assert 'failed 3 times' in client.get('/events').json()[0]['description']


def test_fb2_restore_of_held_down_link_is_explained_noop(api):
    client, s = api
    protected_demo(client)
    for _ in range(3):
        client.post('/links/L3/fail')
        client.post('/links/L3/restore')
    n_events = len(s.events)
    r = client.post('/links/L3/restore').json()
    assert r['changed'] is False and r['event'] == 'link_held_down'
    assert 'more controller step' in r['message']
    assert len(s.events) == n_events


def test_fb2_hold_down_expires_after_cooldown_and_is_logged(api):
    client, s = api
    protected_demo(client)
    for _ in range(3):
        client.post('/links/L3/fail')
        client.post('/links/L3/restore')
    for _ in range(s.flap_cooldown):
        client.post('/simulate')
    assert link_status(client, 'L3')['status'] == 'active'
    assert any(e['event_type'] == 'flap_hold_down_ended' for e in client.get('/events').json())
    # Back in service with a fresh plan: positive guarantees and Reflex again.
    assert client.post('/links/L3/fail').json()['reflex_used'] is True


def test_fb2_link_that_stayed_down_is_not_flapping():
    s = build('AB', [('L1', 'A', 'B', 10, 1)], [])
    for _ in range(2):
        s.bump_step(); s.fail_link('L1'); s.bump_step(); s.restore_link('L1')
    s.bump_step(); s.fail_link('L1')
    for _ in range(s.flap_window + 1):
        s.bump_step()
    assert s.restore_link('L1').status == LinkStatus.ACTIVE


# ── FB-3 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('endpoint', ['/analyze/what-if', '/failures/batch'])
def test_fb3_empty_link_lists_rejected(api, endpoint):
    client, s = api
    protected_demo(client)
    assert client.post(endpoint, json={'link_ids': []}).status_code == 422


# ── FB-4: step accounting ──────────────────────────────────────────────

def test_fb4_every_action_logs_events_at_its_returned_step(api):
    client, s = api
    protected_demo(client)
    checks = [
        client.post('/links/L3/fail').json(),
        client.post('/links/L3/restore').json(),
        client.post('/failures/batch', json={'link_ids': ['L4', 'L5']}).json(),
        client.post('/demands/emergency', json={'id': 'E9', 'source': 'A', 'destination': 'B',
                                                'bandwidth': 1}).json(),
        client.post('/simulate').json(),
    ]
    steps = [r['step'] for r in checks]
    assert steps == sorted(set(steps)), steps
    by_step = {}
    for e in client.get('/events?limit=1000').json():
        by_step.setdefault(e['step'], []).append(e['event_type'])
    for r in checks:
        assert by_step.get(r['step']), (r, by_step)
    assert 'link_failure' in by_step[checks[0]['step']]
    assert 'reflex_recovery' in by_step[checks[0]['step']]


def test_fb4_repeated_fail_is_noop_without_events(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    n = len(s.events)
    r = client.post('/links/L3/fail').json()
    assert r['changed'] is False and r['event'] == 'link_already_failed'
    assert len(s.events) == n
    assert client.post('/links/L1/restore').json()['event'] == 'link_already_active'


# ── FB-5 ───────────────────────────────────────────────────────────────

def test_fb5_unplanned_rows_are_distinguished(api):
    client, s = api
    client.post('/topology/generate', json={'preset': 'normal_campus'})
    client.post('/simulate')
    rows = {r['flow_id']: r for r in client.get('/protection/plan').json()['critical_flows']}
    assert rows['D1']['planned'] is True and rows['D1']['status'] == 'None' and rows['D1']['guaranteed_mbps'] == 0
    assert rows['D8']['planned'] is False and rows['D8']['status'] == 'Not planned'
    assert rows['D8']['protection_pct'] is None


# ── FB-6: plans for the network as it is ───────────────────────────────

def test_fb6_plan_with_failed_link_is_verified_and_honest(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    plan = client.get('/protection/plan').json()
    # The pre-failure plan stays in force for the covered scenario it is now in.
    assert plan['verified'] and plan['in_force'] and plan['current_scenario'] == 'fail_L3'
    assert plan['further_failures_covered'] is False
    client.post('/simulate')  # re-plan with L3 down
    plan = client.get('/protection/plan').json()
    assert plan['verified'] is True and plan['base_unavailable_links'] == ['L3']
    assert plan['current_scenario'] == 'no_failure' and plan['further_failures_covered'] is True
    g = {r['flow_id']: r['guaranteed_mbps'] for r in plan['critical_flows']}
    # With L3 down only A-B-E remains: failing L1 or L2 disconnects A-E, so 0 is optimal.
    assert g['D1'] == 0 and g['P1_DEMO'] == 0
    assert plan['covered_scenarios_count'] == 5  # baseline + 4 usable links


def test_fb6_second_failure_takes_plan_out_of_force(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    r = client.post('/links/L1/fail').json()
    assert r['reflex_used'] is False
    plan = client.get('/protection/plan').json()
    assert plan['in_force'] is False and plan['current_scenario'] is None


def test_fb6_held_down_link_plan_verified(api):
    client, s = api
    protected_demo(client)
    for _ in range(3):
        client.post('/links/L3/fail')
        client.post('/links/L3/restore')
    plan = client.get('/protection/plan').json()
    assert link_status(client, 'L3')['status'] == 'flapping'
    assert plan['verified'] is True and plan['verification_failures'] == []
    assert plan['base_unavailable_links'] == ['L3']


# ── Defects found by the property harness ──────────────────────────────

def test_reflex_preserves_unaffected_critical_traffic(api):
    client, s = api
    client.post('/topology/generate', json={'preset': 'normal_campus'})
    client.post('/simulate')
    before = {d.id: d.allocated_bandwidth for d in s.demands.values()}
    out = run_reflex_recovery({'L1'}, s.protection_plan, s.demands, s.links, s.candidate_tunnels,
                              previous_allocations=s.active_allocations)
    got = {fid: sum(a.values()) for fid, a in out['allocations'].items()}
    assert out['success']
    for fid in ('D1', 'D6', 'D7'):  # their routes do not use L1
        assert got[fid] == pytest.approx(before[fid])


def test_reflex_preserves_normal_traffic_by_priority():
    # X-Y capacity 10. P1 guaranteed 6 on the only path; P3 (3) and P5 (3) both used X-Y before.
    s = build('XY', [('L1', 'X', 'Y', 10, 1)],
              [('C', 'X', 'Y', 6, TrafficCategory.EMERGENCY), ('N3', 'X', 'Y', 3, TrafficCategory.ACADEMIC),
               ('N5', 'X', 'Y', 3, TrafficCategory.BACKGROUND)])
    simulate(s, 'lp')
    plan = s.protection_plan
    plan.per_flow_guaranteed['C'] = 6.0
    plan.per_tunnel_reservations['C'] = {'T_C_1': 6.0}
    prev = {'C': {}, 'N3': {'T_N3_1': 3.0}, 'N5': {'T_N5_1': 3.0}}
    out = run_reflex_recovery(set(), plan, s.demands, s.links, s.candidate_tunnels, previous_allocations=prev)
    got = {fid: sum(a.values()) for fid, a in out['allocations'].items()}
    # Room after the guarantee is 4 Mbps: P3 keeps all 3, P5 gets the remaining 1 (not 2 + 2).
    assert got == pytest.approx({'C': 6.0, 'N3': 3.0, 'N5': 1.0})


def test_install_policy_rejects_worse_candidate_after_failure(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    d = evaluate_install_policy({fid: {} for fid in s.demands}, s.active_allocations, s.demands, s.links,
                                s.candidate_tunnels, s.version, s.version, trigger_reason='link_failure')
    assert not d.should_install and d.decision_type == 'rejected_priority'
    # A candidate is still installed when the current allocation is invalid.
    stale = {fid: dict(a) for fid, a in s.active_allocations.items()}
    s.fail_link('L1')
    d = evaluate_install_policy({fid: {} for fid in s.demands}, stale, s.demands, s.links,
                                s.candidate_tunnels, s.version, s.version, trigger_reason='link_failure')
    assert d.should_install and d.decision_type == 'installed_recovery'


def test_tunnel_ids_keep_their_path_across_topology_edits(api):
    client, s = api
    protected_demo(client)
    before = {t.id: tuple(t.node_path) for ts in s.candidate_tunnels.values() for t in ts}
    client.post('/nodes', json={'id': 'Z', 'name': 'Z'})
    client.post('/links', json={'id': 'L9', 'source': 'A', 'target': 'Z', 'capacity': 100, 'latency': 1})
    client.post('/links', json={'id': 'L10', 'source': 'Z', 'target': 'E', 'capacity': 100, 'latency': 1})
    client.post('/links/L5/fail')
    after = {t.id: tuple(t.node_path) for ts in s.candidate_tunnels.values() for t in ts}
    assert all(after[tid] == path for tid, path in before.items() if tid in after)


@pytest.mark.parametrize('endpoint,body', [('/compare', None), ('/analyze/what-if', {'link_ids': ['L1']}),
                                           ('/analyze/vulnerabilities', None)])
def test_analysis_after_latency_edit_does_not_crash(api, endpoint, body):
    """Editing one link's latency left stale latency on other flows' cached tunnels (HTTP 500)."""
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    client.post('/links/L3/restore')
    assert client.put('/links/L4', json={'latency': 3}).status_code == 200
    if endpoint.endswith('vulnerabilities'):
        r = client.get(endpoint)
    else:
        r = client.post(endpoint, json=body) if body else client.post(endpoint)
    assert r.status_code == 200, r.text


def test_replan_includes_demand_added_after_simulation(api):
    client, s = api
    protected_demo(client)
    client.post('/demands', json={'id': 'P1_NEW', 'source': 'A', 'destination': 'E', 'bandwidth': 5,
                                  'category': 'emergency'})
    rows = {r['flow_id']: r for r in client.post('/protection/replan').json()['critical_flows']}
    assert rows['P1_NEW']['guaranteed_mbps'] == pytest.approx(5)


def test_what_if_predicts_the_controller_outcome(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    client.post('/links/L3/restore')
    for lid in ('L1', 'L4'):
        w = client.post('/analyze/what-if', json={'link_ids': [lid]}).json()
        before = client.get('/metrics').json()['total_delivered']
        assert w['total_delivered_before'] == pytest.approx(before)
        client.post(f'/links/{lid}/fail')
        assert client.get('/metrics').json()['total_delivered'] == pytest.approx(w['total_delivered_after'])
        client.post(f'/links/{lid}/restore')


def test_what_if_reports_already_unavailable_links(api):
    client, s = api
    protected_demo(client)
    client.post('/links/L3/fail')
    w = client.post('/analyze/what-if', json={'link_ids': ['L3']}).json()
    assert w['already_unavailable'] == ['L3'] and w['recovery_mode'] == 'none'
    assert w['total_delivered_after'] == w['total_delivered_before']


def test_added_demand_is_consistent_before_simulation(api):
    client, s = api
    protected_demo(client)
    d = client.post('/demands', json={'id': 'N1', 'source': 'A', 'destination': 'E', 'bandwidth': 7}).json()
    assert d['status'] == 'pending' and d['allocated_bandwidth'] == 0 and d['unserved_bandwidth'] == 7
    assert client.post('/links/L3/fail').json()['violations'] == []


def test_new_link_clears_stale_unreachable_status(api):
    client, s = api
    client.post('/topology', json={'nodes': [{'id': n, 'name': n} for n in 'ABC'],
                                   'links': [{'id': 'L1', 'source': 'A', 'target': 'B', 'capacity': 10, 'latency': 1}]})
    client.post('/demands', json={'id': 'D1', 'source': 'A', 'destination': 'C', 'bandwidth': 5})
    client.post('/simulate')
    assert s.demands['D1'].status == DemandStatus.UNREACHABLE
    client.post('/links', json={'id': 'L2', 'source': 'B', 'target': 'C', 'capacity': 10, 'latency': 1})
    assert s.demands['D1'].status == DemandStatus.PENDING
    client.post('/simulate')
    assert s.demands['D1'].status == DemandStatus.FULLY_SERVED


def test_lp_fallback_is_reported(api, monkeypatch):
    client, s = api
    protected_demo(client)
    monkeypatch.setattr(engine, 'solve_reactive_multicommodity_lp',
                        lambda *a, **k: {'success': False, 'allocations': {}, 'solver_status': 'forced'})
    r = client.post('/simulate?algorithm=lp').json()
    assert r['algorithm'] == 'lp' and r['algorithm_used'] == 'pacar'
    assert any(e['event_type'] == 'lp_fallback' for e in client.get('/events').json())


def test_verified_guarantees_are_honoured_by_every_algorithm(api):
    """Fuzz seed 133: an aggregate-per-class optimum can starve one flow below its guarantee."""
    client, s = api
    client.post('/topology', json={'nodes': [{'id': f'N{i}', 'name': f'N{i}'} for i in range(7)], 'links': [
        {'id': 'L1', 'source': 'N0', 'target': 'N3', 'capacity': 40, 'latency': 4},
        {'id': 'L2', 'source': 'N0', 'target': 'N4', 'capacity': 10, 'latency': 7},
        {'id': 'L3', 'source': 'N0', 'target': 'N5', 'capacity': 20, 'latency': 5},
        {'id': 'L4', 'source': 'N0', 'target': 'N6', 'capacity': 10, 'latency': 4},
        {'id': 'L5', 'source': 'N1', 'target': 'N2', 'capacity': 40, 'latency': 1},
        {'id': 'L6', 'source': 'N1', 'target': 'N3', 'capacity': 1, 'latency': 2},
        {'id': 'L7', 'source': 'N1', 'target': 'N5', 'capacity': 40, 'latency': 6}]})
    client.post('/demands', json={'id': 'D5', 'source': 'N3', 'destination': 'N2', 'bandwidth': 40,
                                  'category': 'emergency'})
    client.post('/demands', json={'id': 'D6', 'source': 'N5', 'destination': 'N3', 'bandwidth': 5,
                                  'category': 'essential'})
    for algorithm in ('lp', 'pacar', 'baseline'):
        client.post(f'/simulate?algorithm={algorithm}')
        plan = client.get('/protection/plan').json()
        g = {r['flow_id']: r['guaranteed_mbps'] for r in plan['critical_flows']}
        assert plan['in_force'] and g['D6'] == pytest.approx(1.0) and g['D5'] == 0
        assert s.demands['D6'].allocated_bandwidth >= 1.0 - 1e-6, algorithm
    # Hand-derived LP optimum subject to D6's 1 Mbps guarantee: D5 = 1 (via L6) + 20 (via L1-L3)
    # minus the 1 Mbps of L3 or L6 that D6's guarantee occupies = 20 Mbps.
    client.post('/simulate?algorithm=lp')
    assert s.demands['D5'].allocated_bandwidth == pytest.approx(20.0, abs=1e-4)


# ── Hand-calculated algorithm examples ─────────────────────────────────

def lp(s):
    s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
    out = solve_reactive_multicommodity_lp(s.demands, s.links, s.candidate_tunnels)
    assert out['success']
    return {fid: sum(a.values()) for fid, a in out['allocations'].items()}, out


def test_hand_shared_capacity_is_bidirectional():
    # One 10 Mbps link; 8 Mbps each way. Shared physical capacity: total 10, not 16.
    s = build('XY', [('L1', 'X', 'Y', 10, 1)],
              [('F', 'X', 'Y', 8, TrafficCategory.ESSENTIAL), ('R', 'Y', 'X', 8, TrafficCategory.ESSENTIAL)])
    got, _ = lp(s)
    assert got['F'] + got['R'] == pytest.approx(10)


def test_hand_lexicographic_priority():
    # Single 20 Mbps path. P1 wants 10, P3 wants 15: P1 gets all 10, P3 the remaining 10.
    s = build('AB', [('L1', 'A', 'B', 20, 1)],
              [('P1', 'A', 'B', 10, TrafficCategory.EMERGENCY), ('P3', 'A', 'B', 15, TrafficCategory.ACADEMIC)])
    got, _ = lp(s)
    assert got == pytest.approx({'P1': 10, 'P3': 10})


def test_hand_multipath_split_reaches_max_flow():
    # Diamond A-B-D / A-C-D, every link 10 Mbps: max flow 20, so a 15 Mbps demand is fully served.
    s = build('ABCD', [('AB', 'A', 'B', 10, 1), ('BD', 'B', 'D', 10, 1), ('AC', 'A', 'C', 10, 2),
                       ('CD', 'C', 'D', 10, 2)], [('F', 'A', 'D', 15, TrafficCategory.GENERAL)])
    got, out = lp(s)
    assert got['F'] == pytest.approx(15)
    # Latency stage: the 2 ms path (via B) is filled first: 10 via B, 5 via C.
    by_path = {tuple(t.node_path): out['allocations']['F'].get(t.id, 0) for t in s.candidate_tunnels['F']}
    assert by_path[('A', 'B', 'D')] == pytest.approx(10) and by_path[('A', 'C', 'D')] == pytest.approx(5)


def test_hand_failed_link_carries_nothing():
    s = build('ABCD', [('AB', 'A', 'B', 10, 1), ('BD', 'B', 'D', 10, 1), ('AC', 'A', 'C', 10, 2),
                       ('CD', 'C', 'D', 10, 2)], [('F', 'A', 'D', 15, TrafficCategory.GENERAL)])
    s.fail_link('AB')
    got, out = lp(s)
    assert got['F'] == pytest.approx(10)
    assert all('AB' not in t.link_ids for t in s.candidate_tunnels['F'] if out['allocations']['F'].get(t.id))


def test_hand_protected_demo_guarantees():
    # Disjoint paths A-B-E (50 Mbps) and A-C-D-E (200 Mbps). Surviving any single failure leaves
    # one path, so protected total <= 50. P1 first: 10; then P2: 40. Reserved on both paths.
    client = None
    s = SimulationState()
    main_state = main.state
    try:
        main.state = s
        client = TestClient(main.app)
        protected_demo(client)
        p = s.protection_plan
        assert p.verified and p.per_flow_guaranteed['P1_DEMO'] == pytest.approx(10)
        assert p.per_flow_guaranteed['D1'] == pytest.approx(40)
        res = {fid: sorted(round(a, 6) for a in r.values()) for fid, r in p.per_tunnel_reservations.items() if r}
        assert res == {'P1_DEMO': [10, 10], 'D1': [40, 40]}
        r = client.post('/links/L3/fail').json()
        m = client.get('/metrics').json()
        assert r['reflex_used'] and r['violations'] == [] and m['critical_delivery_pct'] == 100
        loads = {l['id']: l['allocated_load'] for l in m['link_utilizations']}
        assert loads['L1'] == pytest.approx(50) and loads['L2'] == pytest.approx(50) and loads['L3'] == 0
        assert {d['id']: d['allocated'] for d in m['demand_details']}['D2'] == 0  # P3 preempted
    finally:
        main.state = main_state


# ── Numerical domain and precision ─────────────────────────────────────

@pytest.mark.parametrize('path,payload', [
    ('/links', {'id': 'T', 'source': 'A', 'target': 'E', 'capacity': 0.001, 'latency': 1}),
    ('/links', {'id': 'T', 'source': 'A', 'target': 'E', 'capacity': 1e8, 'latency': 1}),
    ('/links', {'id': 'T', 'source': 'A', 'target': 'E', 'capacity': 10, 'latency': 1e7}),
    ('/demands', {'id': 'T', 'source': 'A', 'destination': 'E', 'bandwidth': 0.001}),
    ('/demands', {'id': 'T', 'source': 'A', 'destination': 'E', 'bandwidth': 1e8}),
])
def test_rates_outside_certifiable_range_rejected(api, path, payload):
    client, s = api
    protected_demo(client)
    before = (dict(s.links), dict(s.demands))
    assert client.post(path, json=payload).status_code == 422
    assert (dict(s.links), dict(s.demands)) == before


def test_small_guarantees_are_not_rounded_to_zero(api):
    client, s = api
    client.post('/topology', json={'nodes': [{'id': n, 'name': n} for n in 'ABCD'], 'links': [
        {'id': lid, 'source': u, 'target': v, 'capacity': 0.01, 'latency': 1}
        for lid, u, v in [('L1', 'A', 'B'), ('L2', 'B', 'D'), ('L3', 'A', 'C'), ('L4', 'C', 'D')]]})
    client.post('/demands', json={'id': 'D1', 'source': 'A', 'destination': 'D', 'bandwidth': 0.01,
                                  'category': 'emergency'})
    client.post('/simulate?algorithm=lp')
    row = client.get('/protection/plan').json()['critical_flows'][0]
    assert row['guaranteed_mbps'] == pytest.approx(0.01)
    assert client.get('/demands').json()['demands'][0]['allocated_bandwidth'] == pytest.approx(0.01)


def test_event_history_is_bounded():
    s = SimulationState()
    for i in range(SimulationState.MAX_EVENTS + 250):
        s.add_event('x', str(i))
    assert len(s.events) == SimulationState.MAX_EVENTS
    assert s.events[-1].description == str(SimulationState.MAX_EVENTS + 249)


def test_plan_with_unrouted_planned_flow_is_not_in_force(api):
    client, s = api
    protected_demo(client)
    client.post('/demands', json={'id': 'P1_NEW', 'source': 'A', 'destination': 'E', 'bandwidth': 5,
                                  'category': 'emergency'})
    plan = client.post('/protection/replan').json()
    assert plan['verified'] and plan['in_force'] is False
    assert plan['not_in_force_reason'] == 'pending_traffic' and plan['pending_flows'] == ['P1_NEW']
    client.post('/simulate')
    plan = client.get('/protection/plan').json()
    assert plan['in_force'] and plan['not_in_force_reason'] is None
    assert s.demands['P1_NEW'].allocated_bandwidth >= 5 - 1e-6


def test_in_force_means_guarantees_are_honoured_now(api):
    client, s = api
    protected_demo(client)
    assert client.get('/protection/plan').json()['in_force'] is True
    # Simulate a defective installation that leaves D1 below its guarantee.
    from app.routing.allocation import apply_allocation_dict
    alloc = {fid: dict(a) for fid, a in s.active_allocations.items()}
    alloc['D1'] = {tid: r / 2 for tid, r in alloc['D1'].items()}
    apply_allocation_dict(s, alloc, s.candidate_tunnels)
    plan = client.get('/protection/plan').json()
    assert plan['in_force'] is False and plan['not_in_force_reason'] == 'guarantee_not_met'
    assert plan['unmet_flows'] == ['D1']
    client.post('/simulate')
    assert client.get('/protection/plan').json()['in_force'] is True
