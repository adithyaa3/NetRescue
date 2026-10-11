# NetRescue Algorithm Explorer: progress report and handoff

Last updated: 2026-10-10 12:50 IST. Branch `feature/algorithm-visualizer`, created locally from `dev2` at `c7bd5c4`. Nothing is committed or pushed. `main`, `dev` and `dev2` are unchanged.

## 1. Status in one paragraph

The implementation is complete: backend tracing, the explorer API, the interactive dashboard view, tests and documentation. On the final code, all backend checks pass and are saved in `qa/algorithm-explorer-evidence/`:
- 290 of 290 backend tests (252 existing + 38 new)
- byte-identical algorithm outputs across 310 scenarios, with tracing off and on
- 5,470 explorer traces with 0 failures

**Not yet done:** both browser suites need one final re-run. The last runs (explorer 77/77 at 12:05, dashboard regression 154/154 at 11:59) predate two small explorer-only JavaScript edits (see §6). The final re-run was attempted at 12:28 and 12:36, but headless Chrome failed to start, and the Mac then froze. Following the instruction to avoid heavy tests, it was not retried.

## 2. What the explorer does

A separate **🧭 Algorithm Explorer** view opens from the dashboard header, or from "Step through in Explorer" on the Algorithm Comparison tab. It shows how the real algorithms make decisions, step by step:

- **Algorithms (only those implemented and used):**
  - Baseline Dijkstra and PACAR: full step trace
  - Reactive LP: stage trace
  - Protection Planner + Guarantee Checker: stage trace plus full checker trace
  - Failure Recovery (coverage check → Reflex or PACAR fallback → Reactive LP → install policy → guarantee enforcement): mixed trace
- **Graph:** source and destination badges; candidate, selected, allocation, reserved/backup and lost paths; bottleneck, saturated and binding links; failed links (✖); per-step link load/capacity and utilization colors; zoom, pan, fit and a label toggle (labels start hidden above 30 links).
- **Playback:** previous, next, play/pause, restart, end, speed (0.5×–4×), step counter, timeline slider, a coloured phase strip, and keyboard controls (← → Space Home End Esc).
- **Panels:**
  - Step: explanation sections, tables, and the recorded event data behind the step
  - Result: backend metrics, per-demand and per-link tables, and verification checks
  - Compare: the three routing algorithms on identical snapshot copies, optionally with the same link failed
  - About: trace level, source file, and what is and isn't recorded
- **Scenario controls:** a frozen snapshot of the live network, an optional link to fail (on a private copy only), and replay of the last live controller action.
- **Read-only:** the explorer calls only `/explorer/*` endpoints and never mutates the live simulation.

## 3. Architecture

| Layer | File | Role |
|---|---|---|
| Recorder | `backend/app/explorer/trace.py` | `TraceRecorder.emit()` stores JSON-safe copies of events (non-finite → null). It never raises; problems go to `errors`. Capped at 20,000 events, with `truncated` flagged. |
| Instrumentation | `routing/{baseline,pacar,reactive_lp,protection_planner,guarantee_checker,reflex,install_policy}.py`, `simulation/engine.py` | Optional `trace=None` parameter. Every recording call is guarded by `if trace is not None`, and nothing is read back from the recorder. The only non-additive changes: `compute_path_score` now delegates to `path_score_breakdown` (the same expression), a few local flags, and `run_pacar`'s candidate list split into two statements. |
| Narrative | `backend/app/explorer/narrative.py` | Turns raw events into steps (title, summary, sections, tables, graph highlights, network state). It uses only recorded values, or explicit explorer checks. |
| Service | `backend/app/explorer/service.py` | Frozen sessions (least-recently-used: 12 sessions, 24 traces each). Runs traces on private copies, re-runs each one untraced and diffs the outcomes, builds result summaries and comparisons, and captures and replays the last live action. |
| Signatures | `backend/app/explorer/signatures.py` | Scenario fingerprint (routing-relevant content; node positions excluded) and outcome signatures for exact comparison. |
| API | `backend/app/explorer/api.py`, `main.py` | `/explorer/*` router. Live endpoints (`/simulate`, `/simulation/step`, `/links/{id}/fail`, `/links/{id}/restore`, `/failures/batch`, `/demands/emergency`) snapshot the state before acting, inside the same re-entrant lock. Responses are unchanged. |
| Frontend | `backend/app/static/index.html` = `frontend/index.html` (byte-identical) | `ax-`-prefixed CSS and markup, plus a second `<script>` block. No routing logic runs in the browser. Stale responses are dropped by request sequence numbers. |

**Fidelity label on every step:**
- `exact`: recorded from the run
- `stage`: one HiGHS LP stage (objective, optimum, status and reported iterations are recorded; solver internals and the non-unique intermediate solutions are not shown)
- `summary`: compact view of a large recorded input
- `verification`: a check computed by the explorer, not an algorithm step

**Honesty constraints applied:**
- PACAR traces the live `run_pacar`. `run_pacar_allocations` is never called by the app, so it is not traced.
- The LP's unused `previous_allocations` and `beta_churn` are documented as unused, and no churn is described.
- Dijkstra's node expansion (inside NetworkX) is not shown.

## 4. API (all read-only with respect to the live network)

| Endpoint | Purpose |
|---|---|
| `GET /explorer/algorithms` | Catalog with trace level, source and fidelity note |
| `GET /explorer/live` | Live fingerprint, version, step and last captured action (used for stale detection) |
| `POST /explorer/sessions` · `GET /explorer/sessions/{id}` | Freeze or describe a snapshot |
| `POST /explorer/sessions/{id}/trace` `{algorithm, failed_link?}` | Trace on a private copy. Returns steps, raw events, result and verification. |
| `POST /explorer/sessions/{id}/compare` `{failed_link?}` | Baseline, PACAR and LP on identical copies |
| `POST /explorer/live-action/trace` | Replay the last live action from its pre-action snapshot and compare with the live outcome |

Errors: 400 (unknown algorithm or link, unusable link, recovery without a link), 404 (session expired, nothing recorded), 422 (malformed body).

## 5. Files

**Modified (11):**
- `backend/app/main.py`
- `backend/app/routing/baseline.py`, `pacar.py`, `reactive_lp.py`, `protection_planner.py`, `guarantee_checker.py`, `reflex.py`, `install_policy.py`
- `backend/app/simulation/engine.py`
- `backend/app/static/index.html`, `frontend/index.html`

**Added:**
- `backend/app/explorer/` (`__init__`, `trace`, `narrative`, `service`, `signatures`, `api`)
- `backend/tests/test_algorithm_explorer.py`
- `qa/frontend-e2e/explorer-e2e.mjs`
- `qa/algorithm-explorer-evidence/` (logs, JSON results, tools, screenshots)
- this report

**Not mine, untouched:** the untracked root `README.md` (12,868 bytes, 10:09).

## 6. Tests and evidence (exact)

| Check | Code tested | Result | Evidence |
|---|---|---|---|
| Backend suite (pytest) | final | **290 passed, 0 failed** (252 existing + 38 explorer) | `algorithm-explorer-evidence/backend-tests.log` (12:34; newest source change 12:20) |
| Algorithm output equivalence vs. pre-change golden (310 cases: 9 presets, protected demo, 300 random networks; every entry point) | final | **byte-identical with trace OFF and trace ON** (MD5 `467d002a…`) | `output-equivalence.txt`, `tools/golden_outputs.py` |
| Explorer service stress (all algorithms, recovery on every link, 210 networks) | final | **5,470 traces, 0 failures**, all untraced re-runs identical, max 60 steps, none > 1 s | `explorer-service-stress.txt` |
| Live concurrency stress ×10 with 3 explorer threads hammering | pre-12:17 (backend unchanged since, except narrative wording) | **3,200/3,200 live requests 200, 0 invariant failures; 6,506/6,506 explorer requests 200** | `concurrency/` |
| Performance vs. pristine `dev2` (25 nodes / 50 links / 30 demands) | pre-12:17 | Live controller actions **+2.0–2.6 ms (~5%)** from the pre-action snapshot; identical results | `performance/summary.txt` |
| Explorer browser suite (`explorer-e2e.mjs`, headless Chrome, isolated port 8013) | 12:05 HTML | **77/77** | `browser/explorer-e2e-results-1205.json`, `browser/screenshots-1205/` |
| Existing dashboard browser suite (`run-e2e.mjs`, unmodified except output paths) | 11:59 HTML | **154/154** | `browser/dashboard-regression-results-1159.json` |
| Final browser re-runs | final | **NOT RUN.** Chrome failed to launch (12:28, 12:36), then the Mac froze. | — |
| Script syntax (`node --check`, both HTML copies, both blocks) | final | OK | this session |
| HTML copies identical (`cmp`) | final | identical | this session |

**Frontend changes after the 12:05 browser run (explorer-only):**
- polling skips while the tab is hidden: `if (!AX.open || document.hidden) return;`
- link labels start hidden when a topology has more than 30 links (`AX.labelsUserSet`, `AX.showLabels`)

**Frontend changes after the 11:59 dashboard run:** the above, plus path-label staggering and failure markers moved above the path overlays. Both are covered by the 12:05 explorer run. No dashboard-level code changed after 11:59.

**What the browser suites verify:**
- at every step of PACAR, Reactive LP, Protection and Recovery (L3), the panel text, every table cell, every link label, every failure marker, the overlay count and the highlighted links equal the backend trace, including backward jumps
- navigation, play/pause and speed, rapid algorithm switching, hypothetical failures, comparison numbers equal to the API, and replay identical to live
- stale-banner detection and re-capture
- three hard refreshes where the dashboard and explorer graphs render with no clicks
- returning to the dashboard with labels equal to the API
- 1280 / 1024 / 760 px layouts with no overflow
- empty and disconnected networks
- only `/explorer/*` requests sent while exploring, and the live fingerprint unchanged

## 7. Bugs found and fixed during testing

1. The new header button overflowed the page by 13 px at 1024×768 (existing suite 153/154). Fixed with a shorter label below 1360 px. At 760 px, header actions now scroll inside the header (the header already overflowed by 29 px on `dev2`).
2. The CSS `display` on `.ax-stale` and `.ax-overlay-msg` overrode the `hidden` attribute, so the stale banner and loading overlay were always visible. Fixed with `.ax-root [hidden] { display: none !important; }`.
3. Reopening the explorer briefly showed the old snapshot's trace. It is now covered by a "Checking…" overlay until freshness is known.
4. Narrative accuracy:
   - Loads on links shared with a failed tunnel were not removed.
   - "Maximise" vs "maximize" was inconsistent.
   - Overclaims were removed: reservations "set aside" capacity, B4 "leaves capacity for everyday traffic", Start Simulation "uses any algorithm", the baseline "never looks at congestion".
   - Exact PACAR score ties were explained as "nothing measurable"; they now name the strict-`<` tie rule (regression test added).
5. Readability: path labels sat on nodes, labels on a shared segment overlapped, the failure ✖ was hidden under overlays, table paths wrapped, and the step caption collided with the zoom tools.
6. The per-session trace cache was unbounded; it is now LRU (24).

## 8. Known limitations and open items

- **Final browser re-run pending** (see §6). This is the only verification gap.
- Each live controller action costs about 2–2.6 ms more (pre-action snapshot for replay).
- Only the last live action can be replayed, and only actions through the six endpoints listed in §3. Topology or demand edits are not replayable actions.
- LP traces show stage results only (by design). Dijkstra's internal expansion is not shown.
- Protection traces collapse scenarios beyond 40 into one summary step. Traces on large networks can reach about 0.9 MB.
- Below about 1000 px the explorer works but is cramped (graph and panel stack). It is designed for desktop widths.
- The circular fallback layout is used when nodes have no distinct canvas positions (for example, networks built by the API at (0, 0)).

## 9. How to run and demonstrate

```sh
cd ~/Desktop/Commitcon/The-Internet-Goes-Down
.venv312/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/ and hard-refresh (Cmd+Shift+R)
```

1. Click **🛡️ Load Protected Demo**, then **🧭 Algorithm Explorer**.
2. PACAR loads. Step to "P1_DEMO: score 2 candidate paths". The latency term is capped for both paths, so spare capacity decides (0.080 vs 0.258).
3. Select **Failure Recovery (Reflex)** with L3. Step through the failure, the coverage check, the Reflex reservations, the LP stages and the install policy ("KEEP CURRENT").
4. Open **Compare** → "Compare on this snapshot".
5. Go **Back to dashboard** and click **Fail L3**. Reopen the explorer: it re-captures, then **▶ Replay with trace** shows the live action, verified identical.

## 10. Commands to finish verification (run one at a time, on a stable machine)

```sh
cd ~/Desktop/Commitcon/The-Internet-Goes-Down
.venv312/bin/python -m pytest backend/tests -q -p no:cacheprovider        # expect 290 passed
# one isolated server (never port 8000):
.venv312/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8013 &
cd qa/frontend-e2e && BASE_URL=http://127.0.0.1:8013 node explorer-e2e.mjs  # writes qa/explorer-e2e-results.json + qa/frontend-screenshots/explorer-*.png
# existing suite WITHOUT overwriting tracked qa files: copy run-e2e.mjs next to a symlink of node_modules,
# change its SHOTS dir and results path, then run it with BASE_URL=http://127.0.0.1:8013
```

If Chrome fails with "Target page, context or browser has been closed", it can't start on this machine right now (seen after the macOS session restart). Retry after a reboot. Check first with `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --version` (installed: 154.0.8037.99; Playwright-core 1.48.2).

## 11. Commit readiness

- **Code:** ready for review.
- **Before pushing:** re-run the two browser suites on the final HTML (§10).
- **When committing**, choose deliberately:
  - leave out the untracked `README.md` unless it is meant to be part of this branch (it was written outside this work)
  - decide whether to include `qa/algorithm-explorer-evidence/`: 6.0 MB in total, of which 5.8 MB is the 15 PNG screenshots in `browser/screenshots-1205/`; the logs, JSON results and tools are small
- No commit or push has been made.
