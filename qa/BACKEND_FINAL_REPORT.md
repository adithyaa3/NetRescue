# NetRescue backend release verification

Backend readiness: **PASS for the existing synchronous hackathon simulator**. End-to-end dashboard readiness remains conditional on Claude's browser verification. This audit used private in-memory states and TestClient; it never connected to, stopped, reset or changed the live server on port 8000.

## Repository and scope

Initial and final branch: `dev`. Initial and final SHA: `ecca49a75a0c7383a42a4be136a65d6ca2cbdb83`. The repository already contained substantial uncommitted routing fixes, frontend changes, tests and QA evidence. Those changes were preserved. Initial and final status and backend hashes are recorded in `backend-initial-state.json` and `backend-final-state.json`. The static dashboard changed concurrently; that file and all frontend files belong to Claude and were not edited by Codex.

No commits, pushes, pulls, fetches, resets, branch changes or destructive restoration occurred. No architecture, capacity model, failure coverage, objective or guarantee definition changed. No new dependencies or system software were installed.

## Confirmed defects fixed

| Defect | Fix and regression |
| --- | --- |
| Node/link PUT bypassed schema validation, permitting negative/nonfinite capacity, invalid types, nonexistent endpoints, self-loops, parallel edges and object ID/key mismatches. | Typed update schemas and validated merged models/topology before mutation; ten atomic invalid-edit cases. |
| Demand endpoint edits accepted nonexistent nodes; explicit null could not clear a latency limit. | Validate endpoints; distinguish omitted fields from explicit null. |
| Invalid topology/import erased or partially replaced the old network. | Validate a private staged replacement before installing it; both import endpoints tested against a full-state fingerprint. |
| Emergency duplicates/missing endpoints raised uncaught errors; unknown batch/What-If links were silently ignored. | HTTP400 with no mutation; validate whole link set and deduplicate IDs. |
| Default generator named a nonexistent preset; invalid algorithm names silently selected PACAR. | Default `normal_campus`; Literal query validation rejects unknown algorithms with422. |
| What-If “before” recomputed PACAR rather than measuring the actual live LP/baseline allocation. | Read actual snapshot metrics; sample L1 and L1+L7 and a multipath LP regression preserve the full live state. Hypothetical after routing continues to use PACAR. |
| Restore of an already-flapping link restarted cooldown. Metrics/read operations could reactivate links without version changes. | Idempotent restore; cooldown transitions happen during simulation steps and advance the version. Read endpoints tested against full-state fingerprints. |
| Heuristic installation mutated state before checking feasibility. LP installation could use candidate metadata different from metrics. | Validate staged heuristic allocations before atomic installation; retain the installed candidate metadata for dictionary allocations. |
| Demand deletion/edit, link deletion/edit and node deletion retained obsolete allocations or phantom load. | Clear affected flow routing and recompute displayed loads. Unaffected flows remain installed; edited flows await explicit simulation. |
| Import restored calculated routes and loads without corresponding allocations. | Import input topology/demands, discard computed routing/load fields, and calculate new routes on simulation. |
| Emergency injection without category produced general P4 traffic. | The emergency endpoint explicitly creates emergency P1 traffic. Regular demand creation retains category-based priority. |
| Blank link/demand IDs were accepted; raw NaN/Infinity validation errors crashed JSON error serialization. | Reject blank IDs and nonfinite values; sanitize validation-error payloads so invalid input returns422. |
| Acceptance test reset a hardcoded live port8000 network. | Same 25-step workflow and assertions now use the actual ASGI API with an isolated state. Standalone invocation runs pytest too. |

Failure evidence: `backend-regressions-before.log` (18 failures), `backend-additional-before.log` (two additional failures), `backend-crud-before.log` (six CRUD/import failures), `backend-final-edge-before.log` (three additional failures), `backend-nonfinite-before.log` (three additional serialization failures plus the still-unfixed three preceding cases). Assertions in earlier tests were not weakened.

## Files changed by this mission

Modified production files:

- `backend/app/main.py`
- `backend/app/models.py`
- `backend/app/state.py`
- `backend/app/analysis/what_if.py`
- `backend/app/analysis/vulnerability.py`
- `backend/app/routing/allocation.py`
- `backend/app/simulation/engine.py`

Modified test infrastructure: `backend/tests/test_acceptance_25steps.py` (private API client; original workflow assertions retained).

Added: `backend/tests/test_backend_release.py` (40 release cases), `qa/BACKEND_HANDOFF.md`, this report, initial/final state JSON, `qa/backend-repeat-demo.py`, `qa/backend-write-final-state.py`, and `qa/backend-*` evidence logs/JSON. Earlier QA scripts were executed without edits. No frontend/browser test or dashboard source was modified by Codex.

## Executed verification

All commands below were run from the repository root using Python3.12.4 in `.venv312`.

```sh
PYTHONPATH=backend .venv312/bin/python -m pytest backend/tests -q --tb=long --durations=5
PYTHONPATH=backend .venv312/bin/python qa/run_numerical.py
PYTHONPATH=backend .venv312/bin/python qa/run_end_to_end.py
PYTHONPATH=backend .venv312/bin/python qa/investigate_sample_protection.py
PYTHONPATH=backend .venv312/bin/python qa/backend-repeat-demo.py
.venv312/bin/python -m pip check
PYTHONPATH=backend .venv312/bin/python -m compileall -q backend/app backend/tests
PYTHONPATH=backend .venv312/bin/python -c 'import app.main, app.routing.protection_planner, app.routing.reactive_lp, app.routing.reflex'
git diff --check
```

Full suite: **209 passed in 2.05s**, zero failures/skips, including isolated 25-step acceptance. Initial isolated baseline:168 passed. Added40 release cases; formerly excluded live acceptance now runs safely. Actual final command output is in `backend-final-tests.log`.

Numerical canonical fixture passed: guarantee6 Mbps, reservations6 Mbps on both paths, Reflex critical6/normal4 Mbps, surviving links10 Mbps, five scenarios pass; supplied0.26-second shortfall integrates to0.78 megabits. This is an explicitly supplied interval, not simulated or measured recovery time.

All nine presets × three algorithms passed: **27 runs**, with independent allocation validation, analysis/state preservation, sequential failures and reverse restoration (`backend-end-to-end-results.json`). The sample investigation confirms all-zero default guarantees with12 legitimate scenarios and positive guarantees on the protected topology (`backend-protection-results.json`).

Fresh protected API demo repeated **three times** and restored successfully (`backend-repeat-demo-results.json`). Dependencies: no broken requirements. Imports, compilation and whitespace checks passed. Existing tests cover solver failures, invalid plans/allocations, randomized topology/capacity checks, stale rejection, latency, local traffic, priority/fairness and capacity invariants.

## Exact protected demo and API contracts

See `BACKEND_HANDOFF.md` for complete endpoint payloads/response fields and frontend-impacting changes. Important flat responses:

- `/protection/plan`: `active`, `verified`, `covered_scenarios_count`, `solver_status`, `critical_flows`; rows contain `flow_id`, `requested_mbps`, `guaranteed_mbps`, `delivered_mbps`, `protection_pct`.
- `/links/L3/fail`: `reflex_used`, `reflex_time_ms`, `violations`, `version`, `step`. Compute runtime is not end-to-end recovery time.
- `/metrics`: `total_delivered`, `critical_delivery_pct`, `demand_details`, `link_utilizations`; allocated bandwidth/load is in Mbps.
- `/analyze/what-if`: `total_delivered_before/after`, `critical_delivered_before/after`, `general_delivered_before/after`, `unreachable_demands`, `congested_demands`, `alternative_routes`, supplementary nested `before_metrics/after_metrics`. No `lost_bandwidth` field. Before minus after is Mbps throughput loss; negative means improvement.
- `/events`: flat event list. `/compare`: nested `pacar`, `reactive_lp`, `baseline` metrics. No restore-all endpoint: restore unavailable links individually.

To reproduce without touching the user's live network:

```sh
PYTHONPATH=backend .venv312/bin/python qa/backend-repeat-demo.py
```

For an intentionally loaded demo in the dashboard/API:

1. `POST /topology/generate` with `{"preset":"congested_shortest"}`.
2. `POST /demands` with `{"id":"P1_DEMO","source":"A","destination":"E","bandwidth":10,"category":"emergency"}`.
3. `POST /simulate?algorithm=pacar`.
4. Read `/protection/plan`: optimal, verified, six scenarios, D1 P2 guarantee40 Mbps and P1_DEMO guarantee10 Mbps. Each has its full guarantee reserved on each alternative path.
5. `POST /links/L3/fail`: Reflex true; D1 delivered40, P1_DEMO10, D2/D3 zero, L1/L2 load50 Mbps each, critical delivery100%, independent validator zero violations.
6. `POST /links/L3/restore`: active, no violations. Reload the preset and add the demo demand to repeat from fresh inputs. Repeated toggling may deliberately enter flapping cooldown.

These live loading commands were **not** sent to port8000 during the audit. The sample-load button selects `normal_campus`, whose critical attachment bridges make zero all-single-failure guarantees correct.

## Startup, limitations and coordination

From the repository root:

```sh
.venv312/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Port8000 is reserved for the user's current demo. Do not start a second process there. To start a separate instance, use port8002. This audit did not restart the live process; a running process without reload needs a user-controlled restart to load these Python changes, which also recreates its in-memory state.

Verified behavior is synchronous, shared physical-link capacity, candidate-limited LP optimization, priority stages P1–P5 then latency, and solver-free traversing Reflex recovery for verified covered cases. No full-duplex model, elapsed-time scheduler, pending installation queue, packet loss simulation, zero-downtime claim, normal-fraction objective or churn objective was added. Default zero guarantees remain mathematically correct. Multiple failures use fallback and are not claimed as single-failure protection.

Python3.13 compatibility, large-network performance and genuine concurrent asynchronous controller behavior remain unverified. Use tested Python3.12.4. Vulnerability ranking continues to compare PACAR hypothetical outcomes; What-If explicitly compares current delivery against hypothetical PACAR recovery.

`qa/FRONTEND_BACKEND_ISSUES.md` was checked repeatedly and did not exist at the final check; no reported backend issue is pending. Claude still owns actual browser interaction, visual QA and interpretation of zero guarantees/compute timing. Node-based existing dashboard handler regressions passed as part of pytest; Codex did not perform a browser interaction. Claude's frontend changes were preserved and are not represented as Codex's work.
