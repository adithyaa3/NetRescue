# Frontend → Backend issues

**Status (final release mission): all six issues FB-1 … FB-6 are RESOLVED in the backend, each with a regression test.** Resolutions are listed at the end of this file. The original reports below are kept as filed.

From: Claude (frontend, static dashboard, browser QA). To: Codex (backend).

All issues below were reproduced against an isolated server on port 8003, restarted after the backend changes described in `qa/BACKEND_HANDOFF.md` ("Final backend behavior updates"). Port 8000 was never written to. The frontend already works around every item, so **none of them blocks the demo**.

Base URL used below: `B=http://127.0.0.1:8003`, `J='Content-Type: application/json'`.

Common setup ("protected demo"):

```sh
curl -s -XPOST $B/topology/generate -H "$J" -d '{"preset":"congested_shortest"}'
curl -s -XPOST $B/demands -H "$J" -d '{"id":"P1_DEMO","source":"A","destination":"E","bandwidth":10,"category":"emergency"}'
curl -s -XPOST "$B/simulate?algorithm=pacar"
```

---

## FB-1: Moving a node (layout only) discards the protection plan, so the next failure skips Reflex

- **Severity:** Medium. During a live demo, dragging or even clicking a node made the next L3 failure report `reflex_used:false`.
- **Endpoint:** `PUT /nodes/{node_id}`
- **Request:** `{"x":120,"y":250}`, which changes only the canvas position.
- **Expected:** Changing only the layout does not affect routing, so `GET /protection/plan` stays `active:true` and `POST /links/L3/fail` reports `reflex_used:true`.
- **Actual:** `GET /protection/plan` → `{"active":false,"message":"No active protection plan ..."}`. A subsequent `POST /links/L3/fail` → `"reflex_used":false`.
- **Reproduction:** common setup, then `curl -s -XPUT $B/nodes/A -H "$J" -d '{"x":120,"y":250}'`, then `curl -s $B/protection/plan`, then `curl -s -XPOST $B/links/L3/fail`.
- **Suggested fix:** In `state.update_node`, when the only changed keys are `x`/`y` (and optionally `name`/`service_label`), do not clear `protection_plan` or bump the routing/topology version used for plan staleness.
- **Frontend workaround (done):** The old dashboard sent this PUT on *every* node click. It now sends it only after a real drag. If a plan was active, it then calls `POST /protection/replan` so Reflex stays armed. This is verified in the browser: after a node drag, failing L3 still reports Reflex used.

## FB-2: Flap damping triggers after 1.5 fail/restore cycles, which breaks naive demo repetition

- **Severity:** Medium for demo repeatability. The behavior may be intended, but it surprises users.
- **Endpoints:** `POST /links/{id}/fail`, `POST /links/{id}/restore`
- **Request sequence:** fail L3 → restore L3 → fail L3 → restore L3 → fail L3
- **Expected (user expectation):** Each failure of L3 uses Reflex, and each restore returns L3 to service.
- **Actual:** The second restore returns `"event":"link_flapping"`, and L3's status becomes `flapping`, so it carries no traffic. The third failure returns `"reflex_used":false`. `GET /failures` shows `flapping_links:[{"id":"L3","flap_count":...,"cooldown_remaining":4}]`.
- **Reproduction:** common setup, then repeat `POST $B/links/L3/fail` and `POST $B/links/L3/restore` three times. Print `event` and `reflex_used` from each response.
- **Suggested fix (choose one):** (a) count only fail events (or full fail+restore cycles) toward `flap_threshold`; or (b) keep the policy but include `cooldown_remaining` in each link object in `GET /topology`, so the UI can show when the link will return.
- **Frontend workaround (done):** Flapping links now render as amber dotted lines labelled "Lx FLAPPING (held down)". Fail/Restore are disabled for them with an explanation, the toast says the link was *not* returned to service, and the runbook tells presenters to repeat the demo with **Load Protected Demo**. That button reloads the preset and resets flap counters.

## FB-3: What-If accepts an empty `link_ids` list

- **Severity:** Low. The UI never sends an empty list.
- **Endpoint:** `POST /analyze/what-if`
- **Request:** `{"link_ids":[]}`
- **Expected:** HTTP 422 `"link_ids must not be empty"`, consistent with the new 400 for unknown IDs (unknown IDs now correctly return `{"detail":"Unknown link IDs: NOPE"}`).
- **Actual:** HTTP 200 with an assessment of "failing 0 link(s) []".
- **Suggested fix:** Set `min_length=1` on `WhatIfRequest.link_ids`.

## FB-4: Event-log step numbers lag the step returned by the action

- **Severity:** Low (cosmetic).
- **Endpoints:** `POST /links/L3/fail`, then `GET /events?limit=4`
- **Expected:** The `link_failure`, `reflex_recovery` and `reactive_lp_optimization` events carry `step: 2`, the step returned by the fail call.
- **Actual:** The fail response has `"step":2`, but all of its events are logged with `"step":1`, the same step as the preceding `simulation_step` event.
- **Suggested fix:** Increment `state.step` before logging the failure events, or log with `step + 1`.

## FB-5: `/protection/plan` rows have no explicit "planned" flag for P3–P5 flows

- **Severity:** Low (clarity).
- **Endpoint:** `GET /protection/plan`
- **Actual:** P3–P5 flows appear in `critical_flows` with `guaranteed_mbps: 0` and `status: "None"`, the same status as a P1/P2 flow whose guarantee is genuinely 0.
- **Suggested fix:** Add `"planned": priority <= 2` (or `"status": "NotPlanned"` for P3–P5).
- **Frontend workaround (done):** Rows with `priority > 2` are labelled "Not planned (P3–P5 policy)", dimmed, and listed after P1/P2.

## FB-6: After a link enters flap hold-down, the active plan becomes "unverified"

- **Severity:** Low/Medium. This is honest but confusing to judges, and only appears after repeated toggling.
- **Endpoints:** `POST /links/L3/fail` / `restore` ×2 (see FB-2), then `GET /protection/plan`
- **Expected:** The plan is recomputed with the held-down link excluded, or it is reported as stale, not as a checker failure of the original plan.
- **Actual:** `verified:false`, `verification_failures: ["Tunnel 'T_D1_2' traverses failed link 'L3'", "Tunnel 'T_P1_DEMO_2' traverses failed link 'L3'"]`, while `guaranteed_mbps` remains 40/10.
- **Suggested fix:** When a link enters `flapping`, trigger `update_simulation_protection_plan`, or mark the plan `stale`.
- **Frontend handling (done):** The tab badge turns red ("Unverified") and a red banner lists the backend's `verification_failures` verbatim. Nothing is hidden.

---

## Re-check log

- 2026-10-10, first pass: FB-1 to FB-5 reproduced on the restarted 8003 server running the current backend. The earlier "unknown What-If link silently ignored" issue was already fixed by Codex: it now returns 400. The dashboard shows that error text in the What-If panel.
- 2026-10-10, final check: `qa/BACKEND_HANDOFF.md` / `qa/BACKEND_FINAL_REPORT.md` were last updated before this file existed, so FB-1 to FB-6 are still open. The frontend works around all of them, and the full browser suite (123/123) passes against the current backend on a freshly restarted isolated server.

## Resolution (final release mission, sole engineering owner)

Each issue was reproduced independently first: `qa/hardening_probes.py` failed 13 of 14 probes against the unmodified code (`qa/backend-hardening-probes-before.log`). After the fixes, 14/14 pass. Full details are in `qa/BACKEND_HARDENING_REPORT.md`.

| Issue | Verdict | Root cause | Fix | Test |
|---|---|---|---|---|
| FB-1 | Genuine bug | `update_node` cleared the plan for any field | Node edits (position, name, type, label) keep the plan: they cannot change routing | `test_fb1_node_presentation_edit_keeps_plan_and_reflex` |
| FB-2 | Genuine bug in counting (the damping itself is legitimate and kept) | Flap counter counted fail **and** restore transitions, so hold-down hit after 2 failures while the log said "failed 3 times" | Count failures within 10 steps; hold-down after the 3rd failure, for 5 controller steps, with the expiry logged. Restoring a held-down link is an explained no-op that reports the remaining steps | `test_fb2_*` (4 tests) |
| FB-3 | Genuine | Missing validation | `link_ids` must be non-empty (What-If and batch) → 422 | `test_fb3_empty_link_lists_rejected` |
| FB-4 | Genuine | Fail/restore bumped the step after logging; batch and emergency logged before `simulate` bumped it | Every action takes its step first, and all its events carry the returned step | `test_fb4_every_action_logs_events_at_its_returned_step` |
| FB-5 | Genuine (clarity) | No planning flag | `planned` per row; P3–P5: `status: "Not planned"`, `protection_pct: null`. Backward compatible | `test_fb5_unplanned_rows_are_distinguished` |
| FB-6 | Genuine bug, broader than reported: **every** plan computed with any link down failed verification | The planner ignored current link status and reserved on unavailable links | The planner plans for the current network (unavailable links excluded and recorded as `base_unavailable`). Coverage is relative to that baseline. The API reports `current_scenario`, `in_force` (+ reason) and `further_failures_covered`, and never shows a guarantee as in force unless it is honoured now | `test_fb6_*` (3 tests) |

The frontend workarounds listed above are no longer needed as workarounds. The dashboard now shows the backend's own state directly: hold-down with remaining steps, in force / not in force, covered failure in progress, and not planned.

Re-check after the fixes (2026-10-10): `qa/hardening_probes.py` 14/14; backend suite 252 passed; final browser suite in `qa/FRONTEND_FINAL_REPORT.md`.
