"""Bounded performance check on a larger user-built network (isolated server only).

Builds a 25-node / 50-link network with 30 demands (10 critical) and times every
controller action through the HTTP API. Writes qa/backend-performance.json.
Usage: BASE_URL=http://127.0.0.1:8003 .venv312/bin/python qa/performance_check.py
"""
import json
import os
import random
import sys
import time
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8003")
if ":8000" in BASE:
    sys.exit("refusing to run against port 8000")


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    t = time.perf_counter()
    with urllib.request.urlopen(r, timeout=120) as resp:
        out = json.loads(resp.read() or b"null")
    return out, (time.perf_counter() - t) * 1000


rng = random.Random(7)
n = 25
nodes = [{"id": f"N{i}", "name": f"N{i}", "x": 0, "y": 0} for i in range(n)]
edges = {tuple(sorted((f"N{i}", f"N{(i + 1) % n}"))) for i in range(n)}
while len(edges) < 50:
    a, b = rng.sample(range(n), 2)
    edges.add(tuple(sorted((f"N{a}", f"N{b}"))))
links = [{"id": f"L{i + 1}", "source": a, "target": b, "capacity": float(rng.choice([50, 100, 200, 500])),
          "latency": float(rng.randint(1, 8))} for i, (a, b) in enumerate(sorted(edges))]
cats = ["emergency"] * 4 + ["essential"] * 6 + ["academic"] * 7 + ["general"] * 7 + ["background"] * 6
demands = []
for i, cat in enumerate(cats):
    a, b = rng.sample(range(n), 2)
    demands.append({"id": f"D{i + 1}", "source": f"N{a}", "destination": f"N{b}",
                    "bandwidth": float(rng.choice([5, 10, 20, 40])), "category": cat,
                    "priority": {"emergency": 1, "essential": 2, "academic": 3, "general": 4, "background": 5}[cat]})
timings = {}
req("POST", "/topology/import", {"nodes": nodes, "links": links, "demands": demands})
for alg in ("pacar", "lp", "baseline", "pacar"):
    res, ms = req("POST", f"/simulate?algorithm={alg}")
    timings[f"simulate_{alg}"] = round(ms, 1)
    assert not res["violations"], res["violations"]
plan, ms = req("GET", "/protection/plan")
timings["get_plan"] = round(ms, 1)
_, ms = req("POST", "/protection/replan")
timings["replan"] = round(ms, 1)
for lid in ("L1", "L2", "L10"):
    res, ms = req("POST", f"/links/{lid}/fail")
    timings[f"fail_{lid}"] = round(ms, 1)
    assert not res["violations"], res["violations"]
_, ms = req("POST", "/analyze/what-if", {"link_ids": ["L20"]})
timings["what_if_single"] = round(ms, 1)
_, ms = req("POST", "/analyze/what-if", {"link_ids": ["L20", "L30"]})
timings["what_if_double"] = round(ms, 1)
_, ms = req("POST", "/compare")
timings["compare"] = round(ms, 1)
for lid in ("L10", "L2", "L1"):
    res, ms = req("POST", f"/links/{lid}/restore")
    timings[f"restore_{lid}"] = round(ms, 1)
    assert not res["violations"], res["violations"]
_, ms = req("GET", "/analyze/vulnerabilities")
timings["vulnerabilities"] = round(ms, 1)
out = {"nodes": n, "links": len(links), "demands": len(demands), "critical": 10,
       "plan_verified": plan.get("verified"), "covered_scenarios": plan.get("covered_scenarios_count"),
       "timings_ms": timings, "max_ms": max(timings.values())}
with open(os.path.join(os.path.dirname(__file__), "backend-performance.json"), "w") as fh:
    json.dump(out, fh, indent=2)
print(json.dumps(out, indent=2))
