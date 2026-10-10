"""Run the canonical example from the repository root with PYTHONPATH=backend."""
import json
from app.models import Node, Link, Demand, TrafficCategory
from app.state import SimulationState
from app.routing.candidate_paths import generate_all_candidate_tunnels
from app.routing.protection_planner import plan_proactive_protection
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.reflex import run_reflex_recovery
from app.routing.validator import validate_allocation_independently
from app.routing.allocation import apply_allocation_dict
from app.analysis.metrics import compute_metrics, integrate_critical_loss

s = SimulationState()
for nid in 'ABCD':
    s.add_node(Node(id=nid, name=nid))
for u, v in [('A','B'), ('B','D'), ('A','C'), ('C','D')]:
    s.add_link(Link(id=u+v, source=u, target=v, capacity=10, latency=1))
s.add_demand(Demand(id='F1', source='A', destination='D', bandwidth=6, category=TrafficCategory.EMERGENCY))
s.add_demand(Demand(id='F2', source='A', destination='D', bandwidth=12))
s.candidate_tunnels = generate_all_candidate_tunnels(s.demands, s.nodes, s.links)
p = plan_proactive_protection(s.demands, s.links, s.candidate_tunnels)
verification = verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels)
assert verification.is_verified
initial = {'F1': {'T_F1_1': 3, 'T_F1_2': 3}, 'F2': {'T_F2_1': 6, 'T_F2_2': 6}}
apply_allocation_dict(s, initial, s.candidate_tunnels)
s.fail_link('BD')
during = compute_metrics(s)['critical_delivered']
out = run_reflex_recovery({'BD'}, p, s.demands, s.links, s.candidate_tunnels, initial, network_version=s.version)
assert out['success']
val = validate_allocation_independently(out['allocations'], s.demands, s.links,
    {t.id:t for ts in s.candidate_tunnels.values() for t in ts})
assert val.is_valid
apply_allocation_dict(s, out['allocations'], s.candidate_tunnels)
interval = .20 + .01 + .05
loss = integrate_critical_loss([(interval, {'F1':6}, {'F1':during})])
assert abs(loss-.78) < 1e-10
print(json.dumps({'guarantees':p.per_flow_guaranteed, 'reservations':p.per_tunnel_reservations,
    'scenarios_passed':verification.scenarios_passed, 'initial':initial, 'failure':'BD',
    'reflex':out['allocations'], 'link_loads':val.link_loads,
    'critical_during_delay_mbps':during, 'critical_after_install_mbps':compute_metrics(s)['critical_delivered'],
    'illustrative_interval_seconds':interval, 'illustrative_loss_megabits':loss,
    'timing_note':'Supplied delay interval; synchronous controller has no event scheduler.'}, indent=2))
