"""Targeted probes for suspected backend defects (run before and after fixes).

Each probe drives the real FastAPI app through an isolated in-memory state
(TestClient + monkeypatched global), never the live server on port 8000.
Usage: PYTHONPATH=backend .venv312/bin/python qa/hardening_probes.py
Prints PASS/FAIL per probe and exits non-zero if any probe fails.
"""
from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient

from app import main
from app.state import SimulationState

RESULTS = []


def fresh():
    st = SimulationState()
    main.state = st
    import app.simulation.engine as engine
    return TestClient(main.app), st


def record(name, ok, detail=""):
    RESULTS.append({"probe": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  -> {detail}"))


def protected_demo(c):
    assert c.post("/topology/generate", json={"preset": "congested_shortest"}).status_code == 200
    assert c.post("/demands", json={"id": "P1_DEMO", "source": "A", "destination": "E",
                                    "bandwidth": 10, "category": "emergency"}).status_code == 200
    assert c.post("/simulate?algorithm=pacar").status_code == 200


def probe_fb1_node_move_keeps_plan():
    c, st = fresh()
    protected_demo(c)
    c.put("/nodes/A", json={"x": 123, "y": 250})
    plan = c.get("/protection/plan").json()
    fail = c.post("/links/L3/fail").json()
    record("FB-1 position-only node update keeps plan and Reflex",
           plan.get("active") is True and fail.get("reflex_used") is True,
           f"plan.active={plan.get('active')} reflex_used={fail.get('reflex_used')}")


def probe_fb2_flap_count_counts_failures():
    c, st = fresh()
    protected_demo(c)
    seq = []
    for _ in range(2):
        seq.append(c.post("/links/L3/fail").json().get("event"))
        seq.append(c.post("/links/L3/restore").json().get("event"))
    link = next(l for l in c.get("/topology").json()["links"] if l["id"] == "L3")
    # Two failures must not be described/treated as three.
    record("FB-2 two fail/restore cycles do not trigger 3-failure hold-down",
           link["status"] == "active" and link["flap_count"] <= 2,
           f"events={seq} status={link['status']} flap_count={link['flap_count']}")


def probe_fb3_empty_what_if():
    c, st = fresh()
    protected_demo(c)
    r = c.post("/analyze/what-if", json={"link_ids": []})
    record("FB-3 empty What-If link list rejected", r.status_code == 422, f"status={r.status_code}")


def probe_fb4_event_steps():
    c, st = fresh()
    protected_demo(c)
    res = c.post("/links/L3/fail").json()
    evs = c.get("/events?limit=50").json()
    action_events = [e for e in evs if e["event_type"] in ("link_failure", "reflex_recovery",
                                                           "reactive_lp_optimization", "uncovered_failure_fallback")]
    steps = {e["step"] for e in action_events}
    record("FB-4 failure events carry the step returned by the action",
           steps == {res["step"]}, f"response step={res['step']} event steps={sorted(steps)}")


def probe_fb5_planned_flag():
    c, st = fresh()
    protected_demo(c)
    rows = c.get("/protection/plan").json()["critical_flows"]
    p3 = [r for r in rows if r["priority"] > 2]
    ok = bool(p3) and all(r.get("planned") is False for r in p3) and \
        all(r.get("planned") is True for r in rows if r["priority"] <= 2)
    record("FB-5 plan rows distinguish unplanned P3-P5 flows", ok,
           json.dumps([{k: r.get(k) for k in ("flow_id", "priority", "status", "planned")} for r in rows]))


def probe_fb6_plan_with_unavailable_link():
    c, st = fresh()
    protected_demo(c)
    c.post("/links/L3/fail")
    c.post("/simulate?algorithm=pacar")  # replans while L3 is down
    plan = c.get("/protection/plan").json()
    g = {r["flow_id"]: r["guaranteed_mbps"] for r in plan["critical_flows"]}
    # With L3 down, a further failure of L1 or L2 disconnects A-E: no positive guarantee is possible.
    record("FB-6 plan computed with a failed link is verified and claims no impossible guarantee",
           plan.get("verified") is True and g.get("D1") == 0 and g.get("P1_DEMO") == 0,
           f"verified={plan.get('verified')} guarantees={g} failures={plan.get('verification_failures')}")


def probe_fb6_flapping_restore():
    c, st = fresh()
    protected_demo(c)
    for _ in range(3):
        c.post("/links/L3/fail")
        c.post("/links/L3/restore")
    plan = c.get("/protection/plan").json()
    link = next(l for l in c.get("/topology").json()["links"] if l["id"] == "L3")
    record("FB-6 plan stays verified (not falsely failed) while a link is held down",
           link["status"] != "flapping" or plan.get("verified") is True,
           f"L3={link['status']} verified={plan.get('verified')} failures={plan.get('verification_failures')}")


def probe_repeated_fail_noop():
    c, st = fresh()
    protected_demo(c)
    c.post("/links/L3/fail")
    n_before = len(c.get("/events?limit=1000").json())
    r = c.post("/links/L3/fail")
    n_after = len(c.get("/events?limit=1000").json())
    record("Repeated fail of an already-failed link does not log a second failure",
           r.status_code == 409 or n_after == n_before,
           f"status={r.status_code} events {n_before}->{n_after}")


def probe_reflex_keeps_unaffected_critical():
    """Reflex must not drop critical traffic that the failure did not touch."""
    from app.routing.reflex import run_reflex_recovery
    c, st = fresh()
    c.post("/topology/generate", json={"preset": "normal_campus"})
    c.post("/simulate?algorithm=pacar")
    before = {d["id"]: d["allocated_bandwidth"] for d in c.get("/demands").json()["demands"]}
    failed = {"L1"}
    out = run_reflex_recovery(failed, st.protection_plan, st.demands, st.links, st.candidate_tunnels,
                              previous_allocations=st.active_allocations)
    reflex = {fid: sum(t.values()) for fid, t in out["allocations"].items()}
    # D1 (EP1->SRV1 via SW1) and D6 (SRV3->GW1 via R2) never touch L1.
    ok = out["success"] and reflex.get("D1", 0) >= before["D1"] - 1e-6 and reflex.get("D6", 0) >= before["D6"] - 1e-6
    record("Reflex preserves unaffected critical allocations",
           ok, f"before D1={before['D1']} D6={before['D6']} reflex D1={reflex.get('D1')} D6={reflex.get('D6')}")


def probe_what_if_matches_controller():
    c, st = fresh()
    protected_demo(c)
    c.post("/links/L3/fail")
    c.post("/links/L3/restore")  # live state is now an LP allocation
    wi = c.post("/analyze/what-if", json={"link_ids": ["L1"]}).json()
    actual = c.post("/links/L1/fail").json()
    m = c.get("/metrics").json()
    record("What-If 'after' matches what the controller actually does",
           abs(wi["total_delivered_after"] - m["total_delivered"]) < 1e-6,
           f"what-if after={wi['total_delivered_after']} actual={m['total_delivered']}")


def probe_install_policy_never_loses_priority():
    """After a failure the final allocation must not deliver less P1 than the
    valid recovery that was already installed (link_failure override)."""
    from app.routing.install_policy import evaluate_install_policy
    from app.routing.candidate_paths import generate_all_candidate_tunnels
    c, st = fresh()
    protected_demo(c)
    c.post("/links/L3/fail")
    cur = st.active_allocations
    worse = {fid: {} for fid in st.demands}  # valid but delivers nothing
    d = evaluate_install_policy(worse, cur, st.demands, st.links, st.candidate_tunnels,
                                expected_version=st.version, current_version=st.version,
                                trigger_reason="link_failure")
    record("Install policy rejects a strictly worse candidate after a failure",
           not d.should_install, f"decision={d.decision_type}: {d.reason}")


def probe_tunnel_ids_stable_after_topology_edit():
    """A tunnel ID must keep meaning the same path after an unrelated topology edit."""
    c, st = fresh()
    protected_demo(c)
    before = {t.id: tuple(t.node_path) for ts in st.candidate_tunnels.values() for t in ts}
    allocated = {fid: dict(a) for fid, a in st.active_allocations.items()}
    c.post("/nodes", json={"id": "Z", "name": "Z", "x": 0, "y": 0})
    c.post("/links", json={"id": "L9", "source": "A", "target": "Z", "capacity": 100, "latency": 1})
    c.post("/links", json={"id": "L10", "source": "Z", "target": "E", "capacity": 100, "latency": 1})
    c.post("/links/L5/fail")  # regenerates candidate tunnels without re-simulating
    after = {t.id: tuple(t.node_path) for ts in st.candidate_tunnels.values() for t in ts}
    changed = [tid for tid in before if tid in after and before[tid] != after[tid]]
    record("Existing tunnel IDs keep their path across regeneration", not changed,
           f"remapped IDs: {changed[:5]}")


def probe_replan_after_new_demand():
    c, st = fresh()
    protected_demo(c)
    c.post("/demands", json={"id": "P1_NEW", "source": "A", "destination": "E", "bandwidth": 5,
                             "category": "emergency"})
    plan = c.post("/protection/replan").json()
    g = {r["flow_id"]: r for r in plan.get("critical_flows", [])}
    record("Recompute Plan includes a newly added critical demand",
           "P1_NEW" in g and g["P1_NEW"]["guaranteed_mbps"] > 0,
           f"P1_NEW row={g.get('P1_NEW')}")


def probe_lp_mode_reports_actual_algorithm():
    c, st = fresh()
    c.post("/topology/generate", json={"preset": "normal_campus"})
    r = c.post("/simulate?algorithm=lp").json()
    record("Simulate reports which algorithm was actually installed",
           "algorithm_used" in r, f"response keys={sorted(r)}")


PROBES = [
    probe_fb1_node_move_keeps_plan, probe_fb2_flap_count_counts_failures, probe_fb3_empty_what_if,
    probe_fb4_event_steps, probe_fb5_planned_flag, probe_fb6_plan_with_unavailable_link,
    probe_fb6_flapping_restore, probe_repeated_fail_noop, probe_reflex_keeps_unaffected_critical,
    probe_what_if_matches_controller, probe_install_policy_never_loses_priority,
    probe_tunnel_ids_stable_after_topology_edit, probe_replan_after_new_demand,
    probe_lp_mode_reports_actual_algorithm,
]

if __name__ == "__main__":
    for p in PROBES:
        try:
            p()
        except Exception as exc:  # a crash is a failure too
            record(p.__name__, False, f"exception: {type(exc).__name__}: {exc}")
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} probes passed")
    sys.exit(1 if failed else 0)
