"""Reproduce protected API demo without sockets or changes to the live server."""
import json
from fastapi.testclient import TestClient
from app import main
from app.state import SimulationState
from app.routing.validator import validate_allocation_independently

records = []
original = main.state
try:
    for iteration in range(3):
        main.state = SimulationState()
        with TestClient(main.app) as api:
            def post(path, payload=None):
                response = api.post(path,json=payload)
                response.raise_for_status()
                return response.json()
            post('/topology/generate',{'preset':'congested_shortest'})
            post('/demands',{'id':'P1_DEMO','source':'A','destination':'E','bandwidth':10,'category':'emergency'})
            healthy = post('/simulate?algorithm=pacar')
            plan = api.get('/protection/plan').json()
            reservations = main.state.protection_plan.per_tunnel_reservations
            failure = post('/links/L3/fail')
            metrics = api.get('/metrics').json()
            lookup = {t.id:t for ts in main.state.candidate_tunnels.values() for t in ts}
            checked = validate_allocation_independently(main.state.active_allocations,main.state.demands,main.state.links,lookup)
            delivered = {d['id']:d['allocated'] for d in metrics['demand_details']}
            loads = {l['id']:l['allocated_load'] for l in metrics['link_utilizations']}
            assert plan['verified'] and plan['covered_scenarios_count'] == 6
            assert failure['reflex_used'] and checked.is_valid
            assert delivered['D1'] == 40 and delivered['P1_DEMO'] == 10
            assert loads['L1'] == loads['L2'] == 50
            restore = post('/links/L3/restore')
            assert main.state.links['L3'].status.value == 'active'
            records.append({'iteration':iteration+1,'healthy':healthy,'plan':plan,'reservations':reservations,
                'failure':failure,'delivered_mbps':delivered,'link_loads_mbps':loads,
                'independent_validator_violations':checked.violations,'restore':restore})
finally:
    main.state = original
print(json.dumps(records,indent=2))
