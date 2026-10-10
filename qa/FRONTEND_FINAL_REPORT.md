# NetRescue frontend final report (final release mission)

Branch `dev`. Nothing was committed, pushed or reset. The dashboard FastAPI serves is `backend/app/static/index.html`; `frontend/index.html` is kept **byte-identical** (it is also the Vite build entry).

All browser testing used real headless **Google Chrome** (Playwright, `playwright-core@1.48.2` installed project-locally) against my isolated server on **port 8003**. Requests to port 8000 were aborted at the network layer: **0 attempts** recorded in every run. The user's live server was never touched.

## 1. Bugs found and fixed in this mission

This list is in addition to the 23 fixes from the previous frontend pass (same-origin API, exact link picker, What-If crash, honest zero guarantees, etc.), all of which remain in place and are re-verified by the suite.

| # | Bug | Root cause | Fix | Evidence |
|---|---|---|---|---|
| F1 | **Intermittent "Connect Nodes modal stays open"** (seen once previously) | A race in `syncState()`. An older sync awaited `/topology` and then `/demands`; when it finished *after* a newer sync it overwrote `state.links` with stale data. The next connect then reused ID `L1`, the backend rejected it, and the modal stayed open | Each sync has a sequence number and only the newest may apply its results (topology, demands, failures, metrics and the protection plan loaded at its end). New link IDs are computed from a fresh `/topology` read | **Deterministic reproduction** (`qa/frontend-e2e/connect-race.mjs`, one slow response injected): the old dashboard fails ("Error: Link 'L1' already exists", modal open); the fixed dashboard passes 3/3 (`qa/final-gate/connect-race.log`) |
| F2 | Double-clicks could send duplicate actions (e.g. the simulation toggle) | No re-entrancy guard on modal submits and toggles | `runExclusive` guard on Start/Stop, Add/Edit Node, Connect/Edit Link, Add/Edit Demand and Recompute Plan. Fail/restore already had a busy guard | Browser: double-clicking Fail sends exactly 1 request; the toggle never sends duplicate simulate requests |
| F3 | The Protection tab claimed "fully protected across every single-link failure" even *during* a failure, when a further failure was not covered | No plan state from the backend | Uses the new `in_force`, `current_scenario`, `further_failures_covered`, `base_unavailable_links` and `not_in_force_reason`: "Covered failure in progress: L3 is down … does not cover a further failure", "Plan not in force" (two failures), "Plan computed, not yet in force" (unrouted demand) and "Guarantee not met right now" | Browser checks for each state + screenshots `41-…`, `14-…` |
| F4 | P3–P5 rows showed "0 Mbps" in the Guarantee column | They are not planned at all | Rows use the backend's `planned`: the Guarantee column shows "—" and the status "Not planned (P3–P5 policy)" | UI = API checks |
| F5 | Held-down links were explained vaguely ("until the cooldown expires") | The backend gave no count or remaining steps | Shows "failed 3 times within 10 controller steps … returns automatically after N more controller steps (each simulation run, failure or restore is one step)" using `/failures` and the restore response | Browser check on the hold-down notice |
| F6 | A backend no-op (repeat fail/restore, held-down restore) was reported as an action | No `changed` field | `changed: false` responses are shown as the backend's message; nothing is recorded as a failure | `api-errors` and `repeat-cycles` checks |
| F7 | What-If did not say how recovery would happen | n/a | Shows "Controller response replayed on a private copy: Reflex … / PACAR fallback …", plus links that were already unavailable | Browser check |
| F8 | Comparison did not label optimality claims | n/a | Shows the backend's `optimality` text ("heuristic (no optimality guarantee)" for PACAR, "lexicographic optimum over candidate tunnels" for the LP) and flags an LP fallback | Browser check |
| F9 | Small non-zero rates could be rendered as "0 Mbps" | Fixed-digit rounding | `fmtMbps` never rounds a non-zero value to 0; guarantees use whole numbers when exact, otherwise 2 decimals | Backend test for 0.01 Mbps guarantees; UI = API checks |
| F10 | Inputs allowed values the backend now rejects (below 0.01 or above 10⁷ Mbps) | Old limits | Client validation and the input `min`/`max` match the backend domain, with clear messages | CRUD checks |
| F11 | New event types had no color | n/a | Colors for hold-down, guarantee enforcement, LP not installed / fallback and emergency demand | Visual check |

## 2. Files changed (frontend)

- `backend/app/static/index.html` and the identical `frontend/index.html`
- `qa/frontend-e2e/run-e2e.mjs`: extended to 154 checks
- `qa/frontend-e2e/connect-race.mjs`: new deterministic race reproduction
- `qa/frontend-screenshots/*.png`: refreshed; new: `13-…`, `14-…`, `40-…`, `41-…`
- `qa/final-gate/browser-*`, `qa/final-gate/connect-race.log`

The backend changes are listed in `qa/BACKEND_HARDENING_REPORT.md`.

## 3. Browser tests actually executed (final code, freshly restarted isolated server)

**Two consecutive full runs: 154/154 and 154/154** (`qa/final-gate/browser-run-1.log`, `-2.log`). The server was started fresh for the gate and reported `version 0, step 0`.

Coverage, all through real clicks:

- **Load and empty states:** initial load, empty state, metrics show "—" when there is no data.
- **Protected demo, twice:** launch, then six checks of what the dashboard displays:
  - guarantees 10/40 and 50/50
  - 6 scenarios
  - Checker PASS
  - "fully protected" banner
  - L3 pre-selected
  - the button reading "Fail L3"

  Then the sequence:
  - pre-failure traffic (UI = API)
  - Fail L3: toast "Reflex used … Validator violations: 0"; inspector Reflex YES
  - canvas: "L3 FAILED"; L1/L2 at 50/50M (100%)
  - P1_DEMO 10 and D1 40 kept; last-failure card "✓ kept"; critical delivery 50/50 (UI = API)
  - Protection tab shows "Covered failure in progress … does not cover a further failure"
  - Event Log shows `link_failure` + `reflex_recovery`
  - What-If L1 replays the controller without changing the live network (API-checked)
  - Comparison labels optimality
  - Restore L3 restores D1 40, D2 30, D3 ≥ 50 (UI = API)
- **Reload:** after reloading the page, the simulation state and the 40 Mbps guarantee are recovered from the backend.
- **Reload during a failure:** the UI still shows L3 failed, the covered scenario and the active simulation (UI = API).
- **Node click and drag before failing L3:** the plan stays active and Reflex is still used; Restore All works.
- **Flap damping:** three fail/restore cycles hold L3 down *exactly* on the 3rd failure, with an explanation of the failure count and remaining steps (UI = API); reloading the demo uses Reflex again.
- **Second failure:** "Reflex NOT used", "Plan not in force", badge "not in force" (UI = API).
- **Double clicks:** Fail sends exactly one request; the simulation toggle sends no duplicate simulate.
- **Sample network:** loads as draft ("Not simulated yet", not "0 Mbps"), shows the zero-guarantee warning and "0 / 65 Mbps", and the P5 row is "Not planned".
- **What-If** with L1 (195 → 195), L1+L7 (195 → 180, D6 and D7 unreachable) and L7 alone.
- **Canvas selection:** clicking L7 fails exactly `["L7"]`, then it is restored from the inspector.
- **CRUD:**
  - Clear; add 3 nodes; duplicate ID rejected; edit node
  - connect L1–L3; parallel link rejected; negative capacity rejected; edit link (80 Mbps / 7 ms)
  - add, edit and validate traffic
  - Recompute Plan before routing reports "not yet in force"
  - simulate; Recompute Plan (T1 guaranteed 25 Mbps)
  - Stop Simulation; delete traffic, link and node
- **Navigation:** zoom in/out/fit/reset, pan by dragging, wheel zoom (the plan is untouched).
- **Export and tray:** Export downloads valid JSON; tray collapse/expand.
- **Errors:** API errors and backend-unreachable messages.
- **Responsive:** at 1280×720 and 1024×768 there is no horizontal overflow and the key controls are visible.

**Separately:**
- Connect Nodes race: old dashboard reproduces the bug; current dashboard passes 3/3.
- Codex's What-If dashboard harness (`backend/tests/test_what_if_dashboard.py`): 6/6 against both HTML copies.

## 4. Console and network errors

The final runs had **0 unexpected console errors, 0 page errors, 0 unexpected failed HTTP requests and 0 requests to port 8000**. The only logged items are the deliberately aborted `/topology` request in the "backend down" test.

## 5. Test and build results

- Browser: 154/154 twice (final code, fresh server).
- Embedded dashboard script: `node --check` OK. Dashboard copies identical.
- `cd frontend && npm run build` (`tsc && vite build`): OK, `dist/index.html 128.16 kB` (gzip 31.81 kB).
- `git diff --check`: clean.

## 6. Remaining issues and limitations

1. Only Chrome (headless) was tested; Safari and Firefox were not, and no human clicked through it.
2. Fonts load from Google Fonts; offline, the page uses system fonts (cosmetic only).
3. Delete and Clear still use the browser's native `confirm()` dialog.
4. `frontend/src/*` is unused Vite boilerplate (the build bundles only `index.html`).
5. The flap hold-down counts controller steps, not seconds. The UI explains this, but a presenter must remember that **Stop Simulation does not advance steps**.

## 7. Readiness

The frontend passes together with the final backend in a clean integrated run. See the final verdict in `BACKEND_HARDENING_REPORT.md` and the handoff.
