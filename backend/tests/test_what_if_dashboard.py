"""Exercise real What-If API responses through both embedded dashboard handlers."""
import copy
import json
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient
from app import main
from app.state import SimulationState

ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = ['backend/app/static/index.html', 'frontend/index.html']


@pytest.fixture
def sample_client(monkeypatch):
    # Keep the user's actual live/global simulation untouched.
    isolated = SimulationState()
    monkeypatch.setattr(main, 'state', isolated)
    with TestClient(main.app) as client:
        assert client.post('/topology/generate', json={'preset': 'normal_campus'}).status_code == 200
        assert client.post('/simulate?algorithm=pacar').status_code == 200
        yield client, isolated


def run_handler(dashboard, response, links, **extra):
    result = subprocess.run(
        ['node', str(Path(__file__).with_name('what_if_dashboard_harness.cjs'))],
        input=json.dumps({'dashboard': str(ROOT / dashboard), 'response': response, 'links': links, **extra}),
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize('dashboard', DASHBOARDS)
@pytest.mark.parametrize('links,after,disrupted', [(['L1'], 195, 0), (['L7'], 180, 2), (['L1', 'L7'], 180, 2)])
def test_what_if_actual_api_response_and_state_preservation(sample_client, dashboard, links, after, disrupted):
    client, state = sample_client
    before_state = copy.deepcopy({k: v for k, v in vars(state).items() if k != '_lock'})
    response = client.post('/analyze/what-if', json={'link_ids': links})
    assert response.status_code == 200
    data = response.json()
    assert data['total_delivered_before'] == 195
    assert data['total_delivered_after'] == after
    assert data['failed_links'] == links
    assert 'lost_bandwidth' not in data
    assert 'disrupted_demands_count' not in data
    assert {k: v for k, v in vars(state).items() if k != '_lock'} == before_state
    output = run_handler(dashboard, data, links)
    assert 'What-if simulation failed' not in output['html'] + output['text']
    assert f'{195-after:.2f} Mbps' in output['html']
    assert f'{after:.2f} Mbps' in output['html']
    assert f'>{disrupted}</span>' in output['html']
    assert output['calls'] == [{'path': '/analyze/what-if', 'body': {'link_ids': links}}]
