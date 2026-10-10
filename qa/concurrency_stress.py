"""Concurrent-request stress test against an ISOLATED NetRescue server (never port 8000).

Eight threads issue random mutating and read requests at the same time (as a
dashboard does: syncs overlapping actions). Afterwards the independent invariant
checker from property_fuzz.py validates the final state, and every response must
be a 2xx/4xx (no 5xx, no dropped connection).

Usage: BASE_URL=http://127.0.0.1:8003 PYTHONPATH=backend .venv312/bin/python qa/concurrency_stress.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import property_fuzz as pf  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8003")
if ":8000" in BASE:
    sys.exit("refusing to run against port 8000")


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # connection problems are failures
        return -1, str(e)


class HttpClient:
    """Minimal adapter so property_fuzz.check_state can read the live server."""
    class R:
        def __init__(self, code, js):
            self.status_code, self._js = code, js
        def json(self):
            return self._js

    def get(self, path):
        return self.R(*req("GET", path))


def worker(seed, n, codes, lock):
    rng = random.Random(seed)
    for _ in range(n):
        links = [f"L{i}" for i in range(1, 6)]
        op = rng.choice(["fail", "restore", "sim", "whatif", "compare", "replan", "get", "get", "move"])
        if op == "fail":
            c, _ = req("POST", f"/links/{rng.choice(links)}/fail")
        elif op == "restore":
            c, _ = req("POST", f"/links/{rng.choice(links)}/restore")
        elif op == "sim":
            c, _ = req("POST", f"/simulate?algorithm={rng.choice(['pacar', 'lp', 'baseline'])}")
        elif op == "whatif":
            c, _ = req("POST", "/analyze/what-if", {"link_ids": [rng.choice(links)]})
        elif op == "compare":
            c, _ = req("POST", "/compare")
        elif op == "replan":
            c, _ = req("POST", "/protection/replan")
        elif op == "move":
            c, _ = req("PUT", f"/nodes/{rng.choice('ABCDE')}", {"x": rng.randint(0, 600), "y": rng.randint(0, 400)})
        else:
            c, _ = req("GET", rng.choice(["/topology", "/demands", "/metrics", "/protection/plan", "/events?limit=50"]))
        with lock:
            codes.append((op, c))


def setup(method, path, body=None):
    code, out = req(method, path, body)
    if code != 200:
        sys.exit(f"setup request {method} {path} failed: status={code} detail={out!r}")


def main():
    t0 = time.time()
    setup("POST", "/topology/generate", {"preset": "congested_shortest"})
    setup("POST", "/demands", {"id": "P1_DEMO", "source": "A", "destination": "E", "bandwidth": 10,
                               "category": "emergency"})
    setup("POST", "/simulate")
    codes, lock = [], threading.Lock()
    threads = [threading.Thread(target=worker, args=(i, int(os.environ.get("OPS", 40)), codes, lock)) for i in range(int(os.environ.get("THREADS", 8)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bad = [(op, c) for op, c in codes if c < 200 or c >= 500]
    failures = []
    pf.check_state(HttpClient(), {}, failures, "after-concurrency")
    # A final simulate must leave a consistent, violation-free state.
    code, res = req("POST", "/simulate")
    if code != 200 or res.get("violations"):
        failures.append(f"final simulate {code} {res}")
    pf.check_state(HttpClient(), {}, failures, "after-final-simulate")
    summary = {"requests": len(codes), "by_status": {}, "server_errors": bad, "invariant_failures": failures,
               "elapsed_s": round(time.time() - t0, 1)}
    for _, c in codes:
        summary["by_status"][str(c)] = summary["by_status"].get(str(c), 0) + 1
    with open(os.path.join(os.path.dirname(__file__), "backend-concurrency-results.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(json.dumps(summary, indent=2))
    sys.exit(1 if bad or failures else 0)


if __name__ == "__main__":
    main()
