"""Reproduce protection results without reading or mutating the live API state."""
import json
import networkx as nx
from app.scenarios.presets import SCENARIOS, load_scenario
from app.state import SimulationState
from app.models import Demand, TrafficCategory
from app.simulation.engine import simulate, handle_link_failure
from app.routing.candidate_paths import build_candidate_graph, generate_all_candidate_tunnels, CandidateTunnel
from app.routing.protection_planner import plan_proactive_protection
from app.routing.guarantee_checker import verify_protection_guarantees
from app.routing.reflex import run_reflex_recovery


def load(preset):
    data = load_scenario(preset)
    s = SimulationState()
    s.load_topology(data['nodes'], data['links'], data['demands'])
    simulate(s)
    return s


def inspect_default():
    s = load('normal_campus')
    p = s.protection_plan
    check = verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels)
    graph, mapping = build_candidate_graph(s.nodes, s.links)
    critical = {}
    complete_candidates = {}
    for fid, demand in s.demands.items():
        all_paths = list(nx.all_simple_paths(graph, demand.source, demand.destination))
        complete_candidates[fid] = [CandidateTunnel(f'all_{fid}_{i}', fid, path,
            [mapping[u, v] for u, v in zip(path, path[1:])],
            sum(graph[u][v]['latency'] for u, v in zip(path, path[1:])))
            for i, path in enumerate(all_paths)]
        if demand.priority.value not in (1, 2):
            continue
        disconnected = []
        for lid, link in s.links.items():
            failed_graph = graph.copy()
            failed_graph.remove_edge(link.source, link.target)
            if not nx.has_path(failed_graph, demand.source, demand.destination):
                disconnected.append(lid)
        critical[fid] = {'priority': demand.priority.value, 'source': demand.source,
            'destination': demand.destination, 'demand_mbps': demand.bandwidth,
            'guarantee_mbps': p.per_flow_guaranteed[fid], 'baseline_delivered_mbps':demand.allocated_bandwidth,
            'candidate_count': len(s.candidate_tunnels[fid]), 'all_simple_path_count': len(all_paths),
            'disconnecting_failures': disconnected,
            'common_candidate_links': sorted(set.intersection(*(set(t.link_ids) for t in s.candidate_tunnels[fid]))),
            'candidates': [t.to_dict() for t in s.candidate_tunnels[fid]]}
    complete_plan = plan_proactive_protection(s.demands, s.links, complete_candidates)
    complete_check = verify_protection_guarantees(complete_plan, s.demands, s.links, complete_candidates)
    return {'nodes':len(s.nodes), 'links':len(s.links), 'demands':len(s.demands),
        'solver_status':p.solver_status, 'covered_scenarios':p.covered_scenarios,
        'guarantees':p.per_flow_guaranteed, 'reservations':p.per_tunnel_reservations,
        'checker':vars(check), 'critical_flows':critical,
        'all_simple_paths_plan':{'guarantees':complete_plan.per_flow_guaranteed, 'checker':vars(complete_check)}}


def inspect_positive(add_emergency=False):
    s = load('congested_shortest')
    if add_emergency:
        s.add_demand(Demand(id='P1_DEMO', source='A', destination='E', bandwidth=10,
                           category=TrafficCategory.EMERGENCY))
        simulate(s)
    p = s.protection_plan
    check = verify_protection_guarantees(p, s.demands, s.links, s.candidate_tunnels)
    reflex = {}
    for lid in s.links:
        out = run_reflex_recovery({lid}, p, s.demands, s.links, s.candidate_tunnels,
                                 previous_allocations=s.active_allocations)
        reflex[lid] = {'success':out['success'], 'critical_mbps':out['critical_recovered_mbps']}
    lower_failure_state = s.snapshot()
    lower_failure = handle_link_failure(lower_failure_state, 'L3')
    failure = handle_link_failure(s, 'L1')
    return {'preset':'congested_shortest', 'solver_status':p.solver_status,
        'guarantees':p.per_flow_guaranteed, 'reservations':p.per_tunnel_reservations,
        'checker':vars(check), 'critical_candidates':[t.to_dict() for t in s.candidate_tunnels['D1']],
        'all_single_failures_reflex':reflex,
        'controller_L1_failure':failure, 'D1_after_L1_mbps':s.demands['D1'].allocated_bandwidth,
        'controller_L3_failure':lower_failure,
        'allocated_after_L3_mbps':{fid:d.allocated_bandwidth for fid,d in lower_failure_state.demands.items()}}


if __name__ == '__main__':
    print(json.dumps({'default':inspect_default(), 'positive_demo':inspect_positive(), 'positive_P1_demo':inspect_positive(True)}, indent=2))
