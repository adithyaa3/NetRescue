"""Protected-demo acceptance test, repeated (isolated in-memory states; never port 8000).

Mode A: 25 repetitions, each in a fresh state.
Mode B: 25 repetitions in ONE state, reloading the demo each time (what a presenter does).
Each repetition runs the full sequence and asserts exact values; the independent
invariant checker from property_fuzz.py runs after every mutating step.
Usage: PYTHONPATH=backend .venv312/bin/python qa/protected_demo_repetitions.py
"""
import copy
import json
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
import property_fuzz as pf  # noqa: E402
from app import main  # noqa: E402
from app.state import SimulationState  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def fingerprint(s):
    return json.dumps({"links": [l.model_dump() for l in s.links.values()],
                       "demands": [d.model_dump() for d in s.demands.values()]}, sort_keys=True, default=str)


def run_once(c, s, tag):
    errors = []

    def need(cond, msg):
        if not cond:
            errors.append(f"{tag}: {msg}")

    def invariants(step):
        pf.check_state(c, {}, errors, f"{tag} {step}")

    # 1-3 load, add P1_DEMO, simulate
    need(c.post("/topology/generate", json={"preset": "congested_shortest"}).status_code == 200, "generate")
    need(c.post("/demands", json={"id": "P1_DEMO", "source": "A", "destination": "E", "bandwidth": 10,
                                  "category": "emergency"}).status_code == 200, "add P1_DEMO")
    sim = c.post("/simulate?algorithm=pacar").json()
    need(sim["violations"] == [], f"simulate violations {sim['violations']}")
    invariants("simulate")
    # 4-7 plan
    plan = c.get("/protection/plan").json()
    g = {r["flow_id"]: r for r in plan["critical_flows"]}
    need(approx(g["D1"]["guaranteed_mbps"], 40), f"D1 guarantee {g['D1']['guaranteed_mbps']}")
    need(approx(g["P1_DEMO"]["guaranteed_mbps"], 10), f"P1 guarantee {g['P1_DEMO']['guaranteed_mbps']}")
    need(plan["covered_scenarios_count"] == 6, f"scenarios {plan['covered_scenarios_count']}")
    need(plan["verified"] is True and plan["solver_status"] == "optimal", "checker PASS / optimal")
    need(plan["in_force"] and plan["further_failures_covered"], "plan in force for the intact network")
    need(g["D2"]["planned"] is False and g["D3"]["planned"] is False, "P3/P4 not planned")
    pre = {d["id"]: d["allocated_bandwidth"] for d in c.get("/demands").json()["demands"]}
    need(approx(pre["P1_DEMO"], 10) and approx(pre["D1"], 40), f"pre-failure delivery {pre}")
    # 8-15 fail L3
    fail = c.post("/links/L3/fail").json()
    need(fail["reflex_used"] is True, "Reflex used")
    need(fail["violations"] == [], f"validator violations {fail['violations']}")
    m = c.get("/metrics").json()
    dd = {d["id"]: d for d in m["demand_details"]}
    loads = {l["id"]: l["allocated_load"] for l in m["link_utilizations"]}
    need(approx(dd["D1"]["allocated"], 40) and approx(dd["P1_DEMO"]["allocated"], 10), "critical kept 40/10")
    need(m["critical_delivery_pct"] == 100, f"critical delivery {m['critical_delivery_pct']}")
    need(loads["L1"] <= 50 + 1e-6 and loads["L2"] <= 50 + 1e-6, f"L1/L2 loads {loads}")
    need(approx(loads["L1"], 50) and approx(loads["L2"], 50) and loads["L3"] == 0, f"loads {loads}")
    need(dd["D2"]["allocated"] == 0 and dd["D3"]["allocated"] == 0, "normal traffic preempted")
    plan = c.get("/protection/plan").json()
    need(plan["in_force"] and plan["current_scenario"] == "fail_L3" and not plan["further_failures_covered"],
         "plan in force for fail_L3, no further failure covered")
    invariants("fail L3")
    # 17 what-if does not modify live state
    before = fingerprint(s)
    w = c.post("/analyze/what-if", json={"link_ids": ["L1"]})
    need(w.status_code == 200 and fingerprint(s) == before, "what-if leaves live state unchanged")
    # 18 compare is isolated and capacity-respecting
    cmp = c.post("/compare").json()
    need(fingerprint(s) == before, "compare leaves live state unchanged")
    for alg, mm in cmp.items():
        need(all(lu["allocated_load"] <= lu["capacity"] + 1e-6 for lu in mm["link_utilizations"]), f"{alg} capacity")
        need(all(lu["allocated_load"] == 0 for lu in mm["link_utilizations"] if lu["status"] != "active"),
             f"{alg} uses failed link")
    # 19-20 restore
    rest = c.post("/links/L3/restore").json()
    need(rest["event"] == "link_restore" and rest["violations"] == [], f"restore {rest['event']}")
    after = {d["id"]: d["allocated_bandwidth"] for d in c.get("/demands").json()["demands"]}
    need(approx(after["D1"], 40) and approx(after["P1_DEMO"], 10) and approx(after["D2"], 30), f"restored {after}")
    need(approx(after["D3"], 80), f"D3 after restore {after['D3']}")
    plan = c.get("/protection/plan").json()
    g = {r["flow_id"]: r["guaranteed_mbps"] for r in plan["critical_flows"]}
    need(plan["verified"] and plan["in_force"] and approx(g["D1"], 40) and approx(g["P1_DEMO"], 10),
         "plan re-established after restore")
    invariants("restore L3")
    return errors


def main_():
    report = {"fresh": [], "same_state": []}
    errors = []
    for i in range(25):
        s = SimulationState()
        main.state = s
        e = run_once(TestClient(main.app), s, f"fresh#{i + 1}")
        report["fresh"].append(not e)
        errors += e
    s = SimulationState()
    main.state = s
    c = TestClient(main.app)
    for i in range(25):
        e = run_once(c, s, f"same-state#{i + 1}")
        report["same_state"].append(not e)
        errors += e
    report["errors"] = errors
    report["passed"] = sum(report["fresh"]) + sum(report["same_state"])
    report["total"] = len(report["fresh"]) + len(report["same_state"])
    with open(os.path.join(os.path.dirname(__file__), "backend-protected-demo-repetitions.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    for e in errors[:20]:
        print("FAIL", e)
    print(f"{report['passed']}/{report['total']} protected-demo repetitions passed")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main_()
