"""Exercise every preset and all routing modes without changing live API state."""
import json
from app.state import SimulationState
from app.scenarios.presets import SCENARIOS, load_scenario
from app.simulation.engine import simulate, handle_link_failure, handle_link_restore, compare_algorithms
from app.routing.allocation import validate_allocation
from app.routing.validator import validate_allocation_independently
from app.analysis.metrics import compute_metrics
from app.analysis.what_if import run_what_if
from app.analysis.vulnerability import rank_vulnerabilities
rows = []
for preset in SCENARIOS:
    for algorithm in ('pacar', 'baseline', 'lp'):
        data = load_scenario(preset)
        s = SimulationState()
        s.load_topology(data['nodes'], data['links'], data.get('demands', []))
        for lid in data.get('fail_links', []):
            s.fail_link(lid)
        out = simulate(s, algorithm)
        assert not out['violations'], (preset, algorithm, out['violations'])
        tunnels = {t.id:t for ts in s.candidate_tunnels.values() for t in ts}
        independent = validate_allocation_independently(s.active_allocations, s.demands, s.links, tunnels)
        assert independent.is_valid, (preset, algorithm, independent.violations)
        metrics = compute_metrics(s)
        assert metrics['total_delivered'] <= metrics['total_requested'] + 1e-5
        before = s.snapshot()
        comparison = compare_algorithms(s)
        run_what_if(s, list(s.links)[:1])
        rank_vulnerabilities(s)
        assert s.demands == before.demands and s.links == before.links
        failures = []
        for lid in list(s.links)[:2]:
            event = handle_link_failure(s, lid)
            assert not event['violations'], event
            failures.append({'link':lid, 'reflex_used':event['reflex_used']})
        for lid in reversed(list(s.links)[:2]):
            event = handle_link_restore(s, lid)
            assert not event['violations'], event
        rows.append({'preset':preset, 'algorithm':algorithm, 'delivered_mbps':metrics['total_delivered'],
                     'plan_verified':bool(s.protection_plan and s.protection_plan.verified),
                     'failures':failures, 'comparisons':list(comparison)})
print(json.dumps({'runs':len(rows), 'results':rows}, indent=2))
