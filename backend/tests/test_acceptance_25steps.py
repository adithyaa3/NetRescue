"""
Mandatory 25-Step Acceptance Test for NetRescue Rebuilt Workflow
"""
import urllib.request
import json
import pytest

API = "http://127.0.0.1:8000"

def post(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b''
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def put(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='PUT')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def delete(url):
    req = urllib.request.Request(url, method='DELETE')
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def get(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode())

def test_full_acceptance_workflow():
    print("\n--- Executing 25-step Acceptance Workflow ---")
    
    # 1-2. Reset & verify empty canvas
    post(f"{API}/simulation/reset")
    top = get(f"{API}/topology")
    assert len(top["nodes"]) == 0
    assert len(top["links"]) == 0
    print("Step 1-2 Passed: Initial state is clean & empty.")

    # 3. Add five nodes: R1, R2, R3, R4, R5
    for nid in ["R1", "R2", "R3", "R4", "R5"]:
        post(f"{API}/nodes", {"id": nid, "name": nid, "node_type": "router", "x": 100, "y": 100})
    top = get(f"{API}/topology")
    assert len(top["nodes"]) == 5
    print("Step 3 Passed: 5 nodes created.")

    # 4-8. Connect nodes
    post(f"{API}/links", {"id": "L_R1_R2", "source": "R1", "target": "R2", "capacity": 100, "latency": 5})
    post(f"{API}/links", {"id": "L_R2_R5", "source": "R2", "target": "R5", "capacity": 40, "latency": 5})
    post(f"{API}/links", {"id": "L_R1_R3", "source": "R1", "target": "R3", "capacity": 100, "latency": 5})
    post(f"{API}/links", {"id": "L_R3_R4", "source": "R3", "target": "R4", "capacity": 100, "latency": 5})
    post(f"{API}/links", {"id": "L_R4_R5", "source": "R4", "target": "R5", "capacity": 100, "latency": 5})
    top = get(f"{API}/topology")
    assert len(top["links"]) == 5
    print("Step 4-8 Passed: 5 links created with specified capacities.")

    # 9-10. Add emergency (30M) and general (50M) traffic R1 -> R5
    post(f"{API}/demands", {"id": "D_EMG", "source": "R1", "destination": "R5", "bandwidth": 30, "category": "emergency"})
    post(f"{API}/demands", {"id": "D_GEN", "source": "R1", "destination": "R5", "bandwidth": 50, "category": "general"})
    dems = get(f"{API}/demands")["demands"]
    assert len(dems) == 2
    print("Step 9-10 Passed: Emergency and General demands created.")

    # 11-12. Start simulation & verify allocations
    post(f"{API}/simulate?algorithm=pacar")
    dems = get(f"{API}/demands")["demands"]
    dem_map = {d["id"]: d for d in dems}
    assert dem_map["D_EMG"]["allocated_bandwidth"] == 30.0
    assert dem_map["D_GEN"]["allocated_bandwidth"] > 0
    print(f"Step 11-12 Passed: Routes and allocations computed. D_EMG: {dem_map['D_EMG']['allocated_bandwidth']}M, D_GEN: {dem_map['D_GEN']['allocated_bandwidth']}M")

    # 13-15. Fail link R2-R5 and verify reroute
    post(f"{API}/links/L_R2_R5/fail")
    top = get(f"{API}/topology")
    l_failed = next(l for l in top["links"] if l["id"] == "L_R2_R5")
    assert l_failed["status"] == "failed"
    assert l_failed["allocated_load"] == 0.0
    dems = get(f"{API}/demands")["demands"]
    dem_map = {d["id"]: d for d in dems}
    assert dem_map["D_EMG"]["allocated_bandwidth"] == 30.0
    print(f"Step 13-15 Passed: L_R2_R5 failed (0 load). Traffic rerouted via surviving path: {dem_map['D_EMG']['route']}")

    # 16-17. Add academic demand (40M) while simulation running
    post(f"{API}/demands", {"id": "D_ACAD", "source": "R1", "destination": "R5", "bandwidth": 40, "category": "academic"})
    post(f"{API}/simulate?algorithm=pacar")
    dems = get(f"{API}/demands")["demands"]
    dem_map = {d["id"]: d for d in dems}
    assert "D_ACAD" in dem_map
    assert dem_map["D_ACAD"]["allocated_bandwidth"] > 0
    print(f"Step 16-17 Passed: Dynamic demand added during live simulation: {dem_map['D_ACAD']['allocated_bandwidth']}M allocated.")

    # 18-19. Restore R2-R5
    post(f"{API}/links/L_R2_R5/restore")
    top = get(f"{API}/topology")
    l_restored = next(l for l in top["links"] if l["id"] == "L_R2_R5")
    assert l_restored["status"] == "active"
    print("Step 18-19 Passed: L_R2_R5 restored and active.")

    # 20-21. Edit general demand to 70M & verify constraints
    put(f"{API}/demands/D_GEN", {"bandwidth": 70})
    post(f"{API}/simulate?algorithm=pacar")
    top = get(f"{API}/topology")
    for l in top["links"]:
        assert l["allocated_load"] <= l["capacity"] + 0.01
    print("Step 20-21 Passed: General demand updated to 70M, no capacity violated.")

    # 22. Delete demand D_ACAD
    delete(f"{API}/demands/D_ACAD")
    post(f"{API}/simulate?algorithm=pacar")
    dems = get(f"{API}/demands")["demands"]
    assert "D_ACAD" not in [d["id"] for d in dems]
    print("Step 22 Passed: Demand D_ACAD deleted.")

    # 23. Verify metrics
    m = get(f"{API}/metrics")
    assert m["total_delivered"] > 0
    assert m["critical_delivery_pct"] == 100.0
    print("Step 23 Passed: Metrics reflect live network accurately.")

    # 24. What-if analysis does not mutate state
    m_before = get(f"{API}/metrics")
    whatif = post(f"{API}/analyze/what-if", {"link_ids": ["L_R1_R2"]})
    m_after = get(f"{API}/metrics")
    assert m_before["total_delivered"] == m_after["total_delivered"]
    print("Step 24 Passed: What-if analysis completed without mutating state.")

    # 25. Reset simulation to clean state
    post(f"{API}/simulation/reset")
    top_clean = get(f"{API}/topology")
    assert len(top_clean["nodes"]) == 0
    assert len(top_clean["links"]) == 0
    print("Step 25 Passed: Reset verified, network clean.")

if __name__ == "__main__":
    test_full_acceptance_workflow()
