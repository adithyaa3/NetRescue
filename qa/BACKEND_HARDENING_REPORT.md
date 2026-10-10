# NetRescue backend hardening report (final release mission)

Scope: the backend (FastAPI, controller and every routing algorithm), its tests, and the integration with the dashboard. Repository `/Users/adharshsundaramurthy/Desktop/Commitcon/The-Internet-Goes-Down`, branch `dev`. Nothing was committed, pushed, reset or stashed. All earlier uncommitted work (by Claude and by Codex) was preserved; a full backup was taken before any edit (`scratchpad/backup-final-start/`: the tracked diff, the untracked files, and a snapshot of `backend/` and `frontend/index.html`).

Every test ran against isolated in-memory states (FastAPI `TestClient`) or my own isolated server on port **8003**. Port 8000 (the user's live demo, PID 15278) was **never contacted, stopped or restarted**. Browser runs aborted any request to port 8000 at the network layer and recorded 0 attempts.

## 1. How defects were found

I did not rely on the existing green test suite alone. I added three independent instruments:

1. **`qa/hardening_probes.py`**: 14 targeted probes, one per suspected defect, run before and after the fixes. Before: **1/14 passed** (`qa/backend-hardening-probes-before.log`). After: **14/14**.
2. **`qa/property_fuzz.py`**: seed-reproducible random networks (3–9 nodes, cycles, bridges, disconnected parts, 1–100 Mbps capacities, latency limits, all priorities, local flows) and random operation sequences. The 17 operation types cover simulate (all 4 algorithms), fail, restore, batch, What-If, compare, replan, add/edit/delete demand, add/delete/edit link, node moves, invalid requests, malformed JSON with NaN/Infinity, and "predict" (a What-If immediately followed by the real failure). After every operation an **independent checker** recomputes, from raw API responses only (never the production validator), the following invariants:
   - shared both-direction link capacity
   - no traffic on failed or held-down links
   - no flow above its demand
   - allocated + unserved = requested
   - tunnel paths are simple, run src→dst and use existing links
   - latency limits are respected
   - reported link loads equal the sum of the tunnels
   - "unreachable" status matches a BFS
   - metrics totals and per-priority sums match
   - event-log step ordering
   - What-If and Compare never mutate live state
   - invalid requests never mutate state
   - verified guarantees never exceed demand, are 0 whenever one more covered failure disconnects the flow, and are honoured whenever the plan says it is in force

   Two reference optimizers, written independently of the production code, give exact optimality checks:
   - an **arc-based multicommodity-flow LP** (no candidate paths at all), solved lexicographically P1→P5;
   - a **path-based LP over all enumerated simple paths** (my own DFS), with latency limits and guarantee floors.

   The two references cross-check each other wherever both apply. A third independent LP re-derives the **protection plan** over all simple paths.
3. **`qa/protected_demo_repetitions.py`**, **`qa/concurrency_stress.py`** and **`qa/performance_check.py`**: the 24-step acceptance sequence repeated, a concurrent-request stress test, and timing on a larger network.

## 2. Defects found and fixed

| # | Defect (reproduced) | Root cause | Fix | Regression test(s) |
|---|---|---|---|---|
| **FB-1** | Moving (or even clicking) a node discarded the protection plan; the next failure ran without Reflex | `state.update_node` set `protection_plan = None` for every field | Node fields (name, type, label, x/y) never affect routing, because links and demands reference immutable node IDs, so the plan is kept | `test_fb1_node_presentation_edit_keeps_plan_and_reflex` (×3) |
| **FB-2** | Two fail/restore cycles triggered hold-down; the event said "failed 3 times" after 2 failures | Flap counter incremented on **both** fail and restore transitions | Count **failures** within the 10-step window. Hold-down happens on the restore after the 3rd failure. A link that then stayed down longer than the window is not "flapping". Hold-down expiry is logged (`flap_hold_down_ended`). Restoring a held-down link is an explained no-op | `test_fb2_*` (4 tests) |
| **FB-3** | What-If (and batch failure) accepted an empty link list | No request validation | `min_length=1` → HTTP 422 | `test_fb3_empty_link_lists_rejected` (×2) |
| **FB-4** | Events of a fail/restore/batch/emergency action carried the previous step | Fail/restore bumped the step at the *end*; batch and emergency logged events before `simulate` bumped it | Every action takes its step first; batch and emergency route at that same step (`_simulate_at_step`) | `test_fb4_every_action_logs_events_at_its_returned_step`; fuzz event-step checks |
| **FB-5** | P3–P5 rows were indistinguishable from P1/P2 flows with a 0 guarantee | No planning flag | Rows carry `planned`; P3–P5 rows have `status: "Not planned"` and `protection_pct: null` | `test_fb5_unplanned_rows_are_distinguished` |
| **FB-6** | **Any** plan computed while a link was failed or held down was "Guarantee Checker FAILED" (Reflex then disabled), and plan semantics after a failure were unclear | Planner ignored current link status: tunnels over unavailable links counted as surviving, so it reserved on them and the checker rejected the plan | The planner plans for the network **as it is**: it excludes unavailable links, records them as `base_unavailable`, and covers each further failure of a usable link. Coverage is decided relative to that baseline (`ProtectionPlan.scenario_for`). The API reports `current_scenario`, `in_force` (with `not_in_force_reason`) and `further_failures_covered` | `test_fb6_*` (3 tests); fuzz plan checks |
| N1 | Reflex **dropped unaffected critical traffic** above the guarantee (normal_campus: fail L1 → D1, D6 30/10 → 0 inside Reflex) and scaled P3/P4/P5 uniformly | Reflex only re-applied guarantees, then preserved normal traffic proportionally | Guarantees first, then surviving previous traffic **strictly by priority** (P1 extra, P2 extra, P3, P4, P5), scaled only where room is short; still solver-free and validated | `test_reflex_preserves_unaffected_critical_traffic`, `test_reflex_preserves_normal_traffic_by_priority` (hand-calculated 6/3/1) |
| N2 | After a failure, the install policy installed **any** valid LP candidate, even one delivering **0 Mbps**, over an already valid recovery | `or trigger_reason == "link_failure"` forced installation | Recovery is installed only when the current allocation is invalid; otherwise the strict P1→P5 comparison decides | `test_install_policy_rejects_worse_candidate_after_failure` |
| N3 | Tunnel IDs silently changed meaning after a topology edit (e.g. `T_D1_1` became a different path), corrupting reservations and allocation lookups | IDs were positional indices recomputed at every regeneration | Stable identity: a known path keeps its ID, new paths never reuse an ID, and tunnels referenced by allocations or the plan are retained while they exist physically | `test_tunnel_ids_keep_their_path_across_topology_edits` |
| N4 | A flow whose 5 shortest *full-graph* paths all crossed a failed link had no LP candidates although a live path existed | Candidates were generated only on the full graph | Candidates = K shortest full-graph paths ∪ K shortest live-graph paths ∪ retained referenced tunnels | Fuzz LP-optimality (349 + exact comparisons) |
| N5 | **HTTP 500** in Compare (and wrong 400s in What-If/vulnerabilities) after editing a link's latency | Other flows' cached tunnels kept stale latency metadata; the validator rejected them | `update_link`/`remove_link`/`remove_node` re-derive cached tunnels (recompute latency, drop paths that no longer exist); analysis snapshots regenerate candidates | `test_analysis_after_latency_edit_does_not_crash` (×3) |
| N6 | **Verified guarantee not honoured by the installed routing**: a planned flow received less than its guarantee while the plan was in force (fuzz seeds 12, 133) | PACAR/baseline/LP (aggregate per class) do not know individual guarantees | Guarantees are commitments. The Reactive LP (simulate and restore) optimizes P1→P5 **subject to the in-force guarantees** (floors). After PACAR/baseline routing, a solver-free enforcement step applies the reservations if needed. `in_force` is reported only when every guarantee is honoured now | `test_verified_guarantees_are_honoured_by_every_algorithm` (hand-derived optimum 20 Mbps); `test_in_force_means_guarantees_are_honoured_now` |
| N7 | What-If "after" disagreed with what the controller then did (7 of ~190 fuzz predictions, e.g. predicted 10 Mbps, delivered 25) | What-If used plain PACAR; the controller uses Reflex + Reactive LP | What-If replays the **real controller** (single failure: Reflex/fallback + LP; multiple: batch) on a private copy, and reports `recovery_mode`, `reflex_used` and `already_unavailable` | `test_what_if_predicts_the_controller_outcome`; fuzz "predict" op: 0 mismatches |
| N8 | Recompute Plan gave a newly added P1 demand a 0 guarantee | Replan reused stale candidate tunnels without the new demand | Replan regenerates candidates first; a plan with unrouted planned flows is `not_in_force_reason: "pending_traffic"` | `test_replan_includes_demand_added_after_simulation`, `test_plan_with_unrouted_planned_flow_is_not_in_force` |
| N9 | A demand added after a simulation had allocated + unserved ≠ requested (controller validator violation); `POST /demands` returned the pre-normalization object | `add_demand` left `unserved_bandwidth=0`; the endpoint returned the wrong object | Pending demands store unserved = requested; the endpoint returns the stored demand | `test_added_demand_is_consistent_before_simulation` |
| N10 | "Unreachable" stayed on a demand after a new link connected it | Status from the last simulation was never invalidated | Adding or re-pointing a link marks unreachable demands pending | `test_new_link_clears_stale_unreachable_status` |
| N11 | Repeated fail/restore re-ran recovery and logged duplicate "failed" events | No idempotency at controller level | Explicit no-ops (`changed: false`, `link_already_failed` / `link_already_active` / `link_held_down`), no step, no events | `test_fb4_repeated_fail_is_noop_without_events` |
| N12 | Simulating with `lp` silently fell back to PACAR but still reported `lp`; Compare showed a PACAR result as "LP" | No reporting | `algorithm_used` + `lp_fallback` event; Compare reports `fallback_used` and an honest `optimality` label | `test_lp_fallback_is_reported` |
| N13 | Sub-0.01 Mbps values: guarantees displayed as 0, a 1e-6 Mbps demand "fully served" with 0.0 allocated, and validator tolerance (1e-5) larger than the capacity | Inputs below the certifiable precision; 2–4 decimal rounding | Domain 0.01 Mbps–10 Tbps for capacity/bandwidth, latency ≤ 10⁶ ms (422 otherwise); 6-decimal values | `test_rates_outside_certifiable_range_rejected` (×5), `test_small_guarantees_are_not_rounded_to_zero` |
| N14 | Unbounded event history | Append-only list | Keep the most recent 5000 events | `test_event_history_is_bounded` |

Two existing assertions were updated deliberately and are documented here. In `test_backend_release.py`, the repeated-restore test now performs 3 cycles (corrected flap policy; its cooldown assertion is unchanged), and the empty What-If list now expects 422 (FB-3). No other assertion was weakened.

## 3. API contract changes (all backward compatible for the shipped dashboard)

- `POST /links/{id}/fail|restore`: new fields `changed`, `protection_scenario` (fail), `status` and `cooldown_remaining` (restore). No-op events: `link_already_failed`, `link_already_active`, `link_held_down` (with `message`). The step in the response equals the step of every event that action logged.
- `GET /protection/plan`: rows gain `planned`. P3–P5 rows have `status: "Not planned"` and `protection_pct: null`. Values have 6 decimals. Plan gains `base_unavailable_links`, `live_unavailable_links`, `current_scenario`, `in_force`, `not_in_force_reason` (`unverified` / `uncovered_failures` / `pending_traffic` / `guarantee_not_met`), `pending_flows`, `unmet_flows` and `further_failures_covered`. `covered_scenarios_count` = baseline + usable links.
- `POST /analyze/what-if`: empty list → 422. `failed_links` echoes the requested links. New fields `already_unavailable`, `recovery_mode` and `reflex_used`. "After" is the controller's real response.
- `POST /failures/batch`: empty list → 422.
- `POST /simulate`: `algorithm_used`.
- `POST /compare`: `optimality` per algorithm, `fallback_used` for the LP.
- `POST /demands`: returns the stored demand (pending → `unserved_bandwidth` = requested).
- Validation: capacity/bandwidth must be within [0.01, 10⁷] Mbps and latency/max_latency ≤ 10⁶ ms, otherwise 422.
- New event types: `flap_hold_down_ended`, `reactive_lp_not_installed`, `guarantee_enforcement`, `guarantee_enforcement_failed`, `lp_fallback`.
- Flap damping: hold-down after **3 failures within 10 controller steps** (previously effectively 2), for 5 steps. A controller step is one simulate, fail, restore, batch or emergency action.

## 4. Mathematical correctness evidence

Hand-calculated examples (all pass, `backend/tests/test_release_hardening.py`):

- **Shared capacity:** one 10 Mbps link with 8 Mbps each way → total exactly 10 (not 16).
- **Lexicographic priority:** a 20 Mbps path with P1 10 and P3 15 → P1 10, P3 10.
- **Multipath max-flow and latency stage:** a diamond with every link 10 Mbps and a 15 Mbps demand → 15, filled as 10 on the 2 ms path and 5 on the 4 ms path.
- **Failed link:** carries nothing; the remaining path delivers 10.
- **Priority-ordered Reflex:** room 4 after a 6 Mbps guarantee → P3 keeps 3, P5 gets 1.
- **Guarantee-constrained optimum:** with D6's verified 1 Mbps guarantee, P1 D5 = 20 Mbps (L6: 1 + L1–L3: 20, minus the 1 Mbps D6 occupies).
- **Protected demo:** both paths are disjoint, so surviving any single failure leaves one path and total protection ≤ 50. P1 first gives 10, then P2 40. Reservations are [10, 10] and [40, 40] Mbps; after an L3 failure, L1 and L2 carry exactly 50 and P3 is preempted.

Independent optimality (final campaign plus earlier campaigns, all on independent references):

- **Reactive LP:** in every instance where all simple paths fit in the candidate set, the production lexicographic optimum equals the independent optimum: 180 + 349 + 242 = **771 exact comparisons, 0 mismatches** after the fixes. In all other instances, production never exceeded the independent optimum (upper-bound check).
- **Protection Planner:** equals the independent all-paths protection LP in 166 + 322 + 252 = **740 exact comparisons**. It never exceeded it, and it is 0 whenever one more single failure disconnects the flow.
- **Cross-check:** arc-based and path-based references agree wherever both apply.

Over the three campaigns after the fixes, 17,600 random operations ran with 0 invariant failures.

## 5. Final results (latest code, final gate)

See `qa/final-gate/` for raw logs.

| Check | Result |
|---|---|
| Full backend suite `PYTHONPATH=backend .venv312/bin/python -m pytest backend/tests -q -rs` | **252 passed, 0 failed, 0 skipped** (209 pre-existing + 43 new) |
| New regression file `test_release_hardening.py` | 43 passed |
| Defect probes `qa/hardening_probes.py` | 14/14 (before fixes: 1/14) |
| Protected-demo acceptance (24 steps), `qa/protected_demo_repetitions.py` | **50/50** (25 fresh states + 25 back-to-back in one state) |
| Randomized/property campaign, final code, seeds 5000–5299 × 20 ops | **6,000 operations, 0 failures**; Reactive LP: 300 instances, 242 exact comparisons, 0 mismatches; Planner: 286 instances, 252 exact comparisons, 0 mismatches (`qa/final-gate/property-fuzz.log`) |
| Earlier campaigns during the fix loop | seeds 1–200 × 18 ops (3,600 ops) and 1000–1399 × 20 ops (8,000 ops): 0 failures |
| Concurrency stress (16 threads × 150 requests on isolated server) | Final gate: 7 runs × 2,400 requests, all 200, 0 invariant failures. **One earlier gate run aborted in my test client's first setup request.** The server log shows that request answered 200 (`qa/final-gate/concurrency-run1-*`), and the client cause is unexplained (no diagnostic was captured; diagnostics are now in place). It did not recur in 7 reruns, including the exact preceding sequence |
| Performance (25 nodes, 50 links, 30 demands, 51 covered scenarios) | every dashboard action ≤ 124 ms; vulnerability ranking (not used by UI) 1.0 s |
| Legacy QA scripts (numerical, end-to-end 27 runs, repeat demo, sample protection) | all exit 0 |
| `compileall`, imports, `pip check` (Python 3.12.4) | OK, no broken requirements |
| `git diff --check` | clean |

## 6. Files modified

**Production:**
- `backend/app/state.py`
- `backend/app/models.py`
- `backend/app/main.py`
- `backend/app/simulation/engine.py`
- `backend/app/routing/candidate_paths.py`
- `backend/app/routing/protection_planner.py`
- `backend/app/routing/reflex.py`
- `backend/app/routing/install_policy.py`
- `backend/app/routing/allocation.py`
- `backend/app/analysis/what_if.py`

**Tests:** `backend/tests/test_backend_release.py` (two documented assertion updates) and the new `backend/tests/test_release_hardening.py`.

**QA:**
- New scripts: `qa/hardening_probes.py`, `qa/property_fuzz.py`, `qa/protected_demo_repetitions.py`, `qa/concurrency_stress.py`, `qa/performance_check.py`
- Logs and results: `qa/backend-*.json`, `qa/backend-hardening-probes-before.log`, `qa/final-gate/*`

The frontend changes are listed in `FRONTEND_FINAL_REPORT.md`.

## 7. Remaining limitations (honest)

1. **The LP is candidate-limited.** It is a lexicographic optimum over the candidate tunnels (≤ 5 full-graph + ≤ 5 live-graph shortest paths per flow, plus retained ones). Exactness was verified when those cover all simple paths; on richer topologies it can be below the true optimum (it is never above it).
2. **PACAR and the baseline are heuristics** with no optimality claim. The UI labels them that way.
3. **Protection covers single failures relative to the plan's baseline.** After a covered failure the plan stays in force for that scenario, but a *further* failure is not covered until the next simulate or restore re-plans. The UI says this explicitly.
4. **Guarantees are commitments.** Best-effort traffic of any class, including P1 above its own guarantee, can yield to another flow's verified guarantee. P1 guarantees themselves are maximized before P2's.
5. **The simulator is flow-level and synchronous.** Reflex and LP times are computation times, not measured network downtime, and there are no packets. Flap cooldown counts controller steps, not seconds.
6. `/analyze/vulnerabilities` still ranks links with PACAR estimates (not used by the dashboard).
7. Only Python 3.12.4 and headless Google Chrome were tested.
8. **The user's live server on port 8000 runs code from before these changes** until the user restarts it (see the runbook).
