"""Release regressions use an isolated API; never connect to demo port 8000."""
import copy
import pytest
from fastapi.testclient import TestClient
from app import main
from app.state import SimulationState
from app.models import LinkStatus

@pytest.fixture
def api(monkeypatch):
    s = SimulationState()
    monkeypatch.setattr(main, 'state', s)
    with TestClient(main.app) as client:
        assert client.post('/topology/generate', json={'preset':'congested_shortest'}).status_code == 200
        yield client, s

def fingerprint(s):
    return copy.deepcopy({k:v for k,v in vars(s).items() if k != '_lock'})

@pytest.mark.parametrize('path,payload', [
    ('/links/L1', {'capacity':-1}), ('/links/L1', {'capacity':'Infinity'}),
    ('/links/L1', {'source':'missing'}), ('/links/L1', {'target':'A'}),
    ('/links/L1', {'source':'C','target':'D'}), ('/links/L1', {'id':'renamed'}),
    ('/nodes/A', {'id':'renamed'}), ('/nodes/A', {'node_type':'invalid'}),
    ('/nodes/A', {'x':'Infinity'}), ('/demands/D1', {'source':'missing'}),
])
def test_invalid_edit_is_atomic(api, path, payload):
    client,s = api
    before = fingerprint(s)
    r = client.put(path, json=payload)
    assert r.status_code in (400,422), r.text
    assert fingerprint(s) == before

@pytest.mark.parametrize('endpoint', ['/topology','/topology/import'])
def test_invalid_replacement_preserves_network(api, endpoint):
    client,s = api
    before = fingerprint(s)
    r = client.post(endpoint,json={'nodes':[{'id':'X','name':'X'}], 'links':[
        {'id':'bad','source':'X','target':'missing','capacity':10,'latency':1}]})
    assert r.status_code == 400
    assert fingerprint(s) == before

@pytest.mark.parametrize('path,payload', [
    ('/demands/emergency', {'id':'D1','source':'A','destination':'E','bandwidth':10,'category':'emergency'}),
    ('/demands/emergency', {'id':'new','source':'missing','destination':'E','bandwidth':10,'category':'emergency'}),
    ('/failures/batch', {'link_ids':['L1','missing']}),
    ('/analyze/what-if', {'link_ids':['L1','missing']}),
])
def test_invalid_action_is_atomic(api,path,payload):
    client,s = api
    before = fingerprint(s)
    r = client.post(path,json=payload)
    assert r.status_code == 400
    assert fingerprint(s) == before

def test_defaults_and_latency_clear(api):
    client,s = api
    assert client.post('/topology/generate',json={}).status_code == 200
    assert len(s.nodes) == 10
    assert client.post('/simulate?algorithm=typo').status_code == 422
    assert client.put('/demands/D1',json={'max_latency':10}).status_code == 200
    assert client.put('/demands/D1',json={'max_latency':None}).status_code == 200
    assert s.demands['D1'].max_latency is None

def test_repeated_restore_does_not_restart_cooldown(api):
    _,s = api
    # Flap damping counts failures: the third failure within the window triggers hold-down.
    for _ in range(3):
        s.fail_link('L1'); s.restore_link('L1')
    assert s.links['L1'].status == LinkStatus.FLAPPING
    before = fingerprint(s)
    s.restore_link('L1')
    assert fingerprint(s) == before

@pytest.mark.parametrize('links',[['L1'],['L1','L7']])
def test_sample_what_if_preserves_entire_state(api,links):
    client,s = api
    client.post('/topology/generate',json={'preset':'normal_campus'})
    client.post('/simulate?algorithm=lp')
    before = fingerprint(s)
    metrics = client.get('/metrics').json()
    r = client.post('/analyze/what-if',json={'link_ids':links})
    assert r.status_code == 200
    result = r.json()
    assert result['total_delivered_before'] == metrics['total_delivered']
    assert result['failed_links'] == links
    assert fingerprint(s) == before

@pytest.mark.parametrize('iteration',range(3))
def test_protected_demo_fresh_and_restore(api,iteration):
    client,s = api
    client.post('/demands',json={'id':'P1_DEMO','source':'A','destination':'E','bandwidth':10,'category':'emergency'})
    assert client.post('/simulate?algorithm=pacar').json()['violations'] == []
    p = s.protection_plan
    assert p.verified and len(p.covered_scenarios) == 6
    assert p.per_flow_guaranteed['D1'] == pytest.approx(40)
    assert p.per_flow_guaranteed['P1_DEMO'] == pytest.approx(10)
    r = client.post('/links/L3/fail').json()
    assert r['reflex_used'] and r['violations'] == []
    m = client.get('/metrics').json()
    assert m['critical_delivery_pct'] == 100
    assert s.links['L1'].allocated_load == pytest.approx(50)
    assert s.links['L2'].allocated_load == pytest.approx(50)
    assert client.post('/links/L3/restore').json()['violations'] == []
    assert s.links['L3'].status == LinkStatus.ACTIVE

def test_analysis_reads_never_reactivate_live_links(api):
    client,s = api
    client.post('/simulate')
    s.links['L1'] = s.links['L1'].model_copy(update={'status':LinkStatus.FLAPPING,'last_flap_step':0,'flap_count':3})
    s.step = s.flap_cooldown
    before = fingerprint(s)
    for endpoint in ['/metrics','/analyze/vulnerabilities','/export','/failures']:
        assert client.get(endpoint).status_code == 200
        assert fingerprint(s) == before
    assert client.post('/compare').status_code == 200
    assert fingerprint(s) == before
    s.bump_step()
    assert s.links['L1'].status == LinkStatus.ACTIVE
    assert s.version > before['version']

def test_single_path_installation_rejects_infeasible_result_atomically(api):
    from app.routing.allocation import apply_routing_results
    _,s = api
    before = fingerprint(s)
    bad = s.demands['D1'].model_copy(update={'route':['A','B','E'],'allocated_bandwidth':100})
    with pytest.raises(ValueError):
        apply_routing_results(s,[bad])
    assert fingerprint(s) == before

def test_what_if_before_uses_live_multipath_allocation(api):
    client,s = api
    client.put('/demands/D1',json={'bandwidth':100})
    client.post('/simulate?algorithm=lp')
    m = client.get('/metrics').json()
    result = client.post('/analyze/what-if',json={'link_ids':['L1']}).json()
    assert result['total_delivered_before'] == m['total_delivered']
    assert result['critical_delivered_before'] == m['critical_delivered']

@pytest.mark.parametrize('action', ['delete_demand','edit_demand','delete_link','edit_link','delete_node'])
def test_input_edits_do_not_leave_phantom_load_or_stale_delivery(api,action):
    client,s = api
    client.post('/simulate?algorithm=lp')
    if action == 'delete_demand':
        assert client.delete('/demands/D1').status_code == 200
        assert 'D1' not in s.active_allocations
    elif action == 'edit_demand':
        assert client.put('/demands/D1',json={'destination':'B'}).status_code == 200
        assert client.get('/metrics').json()['demand_details'][0]['allocated'] == 0
    elif action == 'delete_link':
        assert client.delete('/links/L1').status_code == 200
    elif action == 'edit_link':
        assert client.put('/links/L1',json={'capacity':1}).status_code == 200
        assert s.links['L1'].allocated_load <= 1
    else:
        assert client.delete('/nodes/B').status_code == 200
    from app.routing.validator import validate_allocation_independently
    lookup = {t.id:t for ts in s.candidate_tunnels.values() for t in ts}
    checked = validate_allocation_independently(s.active_allocations,s.demands,s.links,lookup)
    assert checked.is_valid, checked.violations
    for lid,load in checked.link_loads.items():
        assert s.links[lid].allocated_load == pytest.approx(load)

def test_export_import_discards_computed_routing_and_replans(api):
    client,s = api
    client.post('/simulate')
    exported = client.get('/export').json()
    payload = {**exported['topology'],'demands':exported['demands']}
    assert client.post('/topology/import',json=payload).status_code == 200
    assert all(l.allocated_load == 0 for l in s.links.values())
    assert all(d.allocated_bandwidth == 0 for d in s.demands.values())
    assert client.post('/simulate').json()['violations'] == []

def test_emergency_endpoint_really_injects_p1(api):
    client,s = api
    r = client.post('/demands/emergency',json={'id':'urgent','source':'A','destination':'E','bandwidth':10})
    assert r.status_code == 200
    assert s.demands['urgent'].category.value == 'emergency'
    assert s.demands['urgent'].priority.value == 1

@pytest.mark.parametrize('endpoint,payload', [
    ('/links',{'id':' ','source':'A','target':'E','capacity':10,'latency':1}),
    ('/demands',{'id':' ','source':'A','destination':'E','bandwidth':10}),
])
def test_empty_ids_rejected(api,endpoint,payload):
    client,s = api
    before = fingerprint(s)
    assert client.post(endpoint,json=payload).status_code in (400,422)
    assert fingerprint(s) == before

def test_empty_network_api_and_invalid_inputs(monkeypatch):
    monkeypatch.setattr(main,'state',SimulationState())
    with TestClient(main.app) as client:
        for algorithm in ['pacar','baseline','lp','reactive_lp']:
            assert client.post('/simulate',params={'algorithm':algorithm}).json()['violations'] == []
        assert client.get('/metrics').json()['total_delivered'] == 0
        assert client.get('/protection/plan').json()['active'] is False
        assert client.post('/protection/replan').status_code == 200
        assert client.post('/compare').status_code == 200
        # FB-3: a What-If must name at least one link.
        assert client.post('/analyze/what-if',json={'link_ids':[]}).status_code == 422
        assert client.get('/analyze/vulnerabilities').json() == []
        assert client.get('/events').status_code == 200
        assert client.get('/export').status_code == 200
        assert client.post('/links/missing/fail').status_code == 400
        assert client.post('/links/missing/restore').status_code == 400
        assert client.post('/topology/generate',json={'preset':'missing'}).status_code == 400
        assert client.post('/demands',json={}).status_code == 422
        assert client.post('/failures/batch',json={}).status_code == 422

def test_restore_all_and_isolated_read_endpoints(api):
    client,s = api
    client.post('/simulate')
    assert client.post('/failures/batch',json={'link_ids':['L1','L3','L1']}).status_code == 200
    assert s.links['L1'].status == s.links['L3'].status == LinkStatus.FAILED
    for lid in ['L1','L3']:
        assert client.post(f'/links/{lid}/restore').json()['violations'] == []
    assert all(l.status == LinkStatus.ACTIVE for l in s.links.values())
    before = fingerprint(s)
    for method,path,payload in [('get','/topology',None),('get','/demands',None),('get','/events',None),
        ('get','/export',None),('get','/metrics',None),('get','/protection/plan',None),
        ('post','/compare',None),('post','/analyze/what-if',{'link_ids':['L1','L3']}),
        ('get','/analyze/vulnerabilities',None)]:
        assert client.request(method,path,json=payload).status_code == 200
        assert fingerprint(s) == before

@pytest.mark.parametrize('endpoint,payload', [('/nodes',{'id':'new','x':float('inf')}),
    ('/links',{'id':'new','source':'A','target':'E','capacity':float('inf'),'latency':1}),
    ('/demands',{'id':'new','source':'A','destination':'E','bandwidth':float('nan')})])
def test_nonfinite_json_returns_validation_error_not_500(api,endpoint,payload):
    import json
    client,s = api
    before = fingerprint(s)
    r = client.post(endpoint,content=json.dumps(payload),headers={'Content-Type':'application/json'})
    assert r.status_code == 422
    assert r.json()['detail']
    assert fingerprint(s) == before
