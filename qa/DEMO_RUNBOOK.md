# NetRescue demo runbook (final)

Every value below was observed in a real browser (headless Google Chrome) driving the final dashboard against the final backend on a fresh isolated server (`qa/frontend-e2e/run-e2e.mjs`). The same sequence also passed 50 scripted API repetitions (`qa/protected_demo_repetitions.py`).

## 1. Start the final application (do this once, ~10 minutes before presenting)

Your current port-8000 server (started earlier) is still running **older backend code**. It must be restarted to load the fixes. Restarting clears the in-memory network, which is expected; the demo button rebuilds it.

1. In the terminal where the port-8000 server is running, press **Ctrl+C** and wait for the prompt.
2. Start the final version:

```sh
cd /Users/adharshsundaramurthy/Desktop/Commitcon/The-Internet-Goes-Down
.venv312/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

   If you prefer your previous style (running from inside `backend/`), use `cd backend && ../.venv312/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.

3. Check it in a second terminal: `curl -s http://127.0.0.1:8000/health` should print `{"status":"ok","version":0,"step":0}`. Version 0 / step 0 confirms a fresh process.
4. Open **http://127.0.0.1:8000/** and press **Cmd+Shift+R** to hard-refresh.

If you can't find the old terminal: `ps -axo pid,command | grep "uvicorn app.main:app" | grep "port 8000"` shows the PID, and `kill <PID>` stops only that process. Then do step 2.

Use a 1280×720 or larger window. Full screen works, and the network refits automatically.

## 2. Click-by-click protected demo

| # | Action | Expected on screen (verified) |
|---|---|---|
| 1 | Click **🛡️ Load Protected Demo** (green, top right) | The toast lists the guarantees "D1 40 Mbps, P1_DEMO 10 Mbps" and says L3 is pre-selected. The simulation starts automatically and the **Protection Guarantees** tab opens. |
| 2 | Show the Protection Guarantees tab | Green banner: **"All 2 P1/P2 flows are fully protected: 50 of 50 Mbps"**. Cards: Guaranteed **50 / 50 Mbps** (In force), Covered scenarios **6** (baseline + 5 single-link failures), LP **OPTIMAL**, Guarantee Checker **PASS**. Rows: **P1_DEMO 10 Mbps 🛡️ FULL**, **D1 40 Mbps 🛡️ FULL**. D2/D3 show "Not planned (P3–P5 policy)". |
| 3 | **⚡ Traffic Demands** tab (pre-failure) | P1_DEMO 10.0, D1 40.0 and D2 30.0 Mbps are Fully Served; D3 is 50.0 of 80 Mbps. On the canvas, **L1 · 50/50M (100%)** and **L2 · 50/50M (100%)**; L3/L4/L5 show 80/200M (40%). |
| 4 | *(Optional, shows robustness)* Click or drag a node | Nothing changes in the plan; positions are cosmetic. |
| 5 | The left picker already shows `L3 · A↔C · 200 Mbps`. Click **💥 Fail L3** | L3 turns grey and dashed with a red ✖ and the label **"L3 FAILED"**. Toast: **"L3 (A↔C) FAILED. Reflex used. Validator violations: 0."** |
| 6 | Point at the right inspector (Last failure card) | **Reflex used: ✅ YES (precomputed protection plan)**, Reflex compute time ≈ 0.05 ms, **Validator violations: 0**. Before → After: **P1_DEMO 10 → 10**, **D1 40 → 40**. |
| 7 | Point at the canvas and Traffic Demands | **L1 · 50/50M (100%)** and **L2 · 50/50M (100%)**. P1_DEMO 10.0 and D1 40.0 Mbps are served on A→B→E. D2 and D3 show **Dropped (congestion)**: lower-priority traffic yields. |
| 8 | **🛡️ Protection Guarantees** tab | Badge "(Verified · 50 Mbps · L3 down)". Banner: **"Covered failure in progress: L3 is down. The verified guarantees (50 of 50 Mbps) are being honoured now. This single-failure plan does not cover a further failure…"** |
| 9 | **📊 Resilience Metrics** | Critical (P1–P2) delivered **100%** ("50 / 50 Mbps now"), Failed links: 1 (Down: L3). The Last Failure card shows "✓ kept" for both flows. |
| 10 | **📜 Event Log** | Newest first, all at the same step: `reactive_lp_not_installed` (the LP found nothing better), **`reflex_recovery`: restored 50.0 Mbps critical traffic (50.0 Mbps from verified guarantees)**, **`link_failure`: Link A↔C (L3) failed.** |
| 11 | **🔬 What-If Analysis**: tick **L1**, then **Evaluate Impact** | Delivered **50.00 → 0.00 Mbps**: with L3 already down, losing L1 would disconnect A from E. The assessment says "Controller response replayed on a private copy: PACAR fallback…" (a second failure is not covered) and **"✓ Live network unchanged"**. Then click **Clear Selection**. |
| 12 | **⚖ Algorithm Comparison** → **Run Comparison** | Three cards evaluated "with failed link(s): L3". PACAR is labelled *heuristic (no optimality guarantee)*, the LP *lexicographic optimum over candidate tunnels*. |
| 13 | Click **🔧 Restore L3** (left panel) | Toast "L3 (A↔C) restored. Validator violations: 0." Afterwards D1 40, D2 30, P1_DEMO 10 and D3 **80** Mbps (D3 split over both paths). The Protection tab is back to "fully protected 50 of 50". |
| 14 | To repeat, click **🛡️ Load Protected Demo** again | The network is rebuilt; all counters reset. |

## 3. What to tell judges

- **The guarantee is computed before the failure.** The Protection Planner LP reserves bandwidth so that P1_DEMO keeps 10 Mbps and D1 keeps 40 Mbps under *every* single-link failure. An independent Guarantee Checker re-verifies all 6 scenarios.
- **Reflex is solver-free.** On a covered failure it switches to the precomputed reservations; no LP runs before critical traffic is restored. The Reactive LP afterwards only installs a result if it is strictly better by priority. The 0.05 ms is computation time in this simulator, **not measured network downtime**.
- **Capacity is shared and real.** After L3 fails, the 50 Mbps path is exactly full (10 + 40), and lower-priority traffic yields.
- **It is honest.** During a failure the dashboard says the plan does not cover a second failure; fail L1 too and it says **"Plan not in force"**. On **🧪 Load Sample Network → Start Simulation** it says **"Verified, but 0 Mbps is guaranteed"**: every critical flow there is single-homed, so zero is the provable best.

## 4. Failing a specific link

- **Picker (most reliable):** choose e.g. `L3 · A↔C · 200 Mbps`, then click **Fail L3**. The button always names its target. Double-clicks are ignored (one request).
- **Canvas:** click a line or its label (labels start with the link ID). The inspector then shows "Link: L3" with Fail/Restore buttons.
- **Colors:** solid red = congested but **up**; grey dashed with a red ✖ = **FAILED**; amber dotted = **FLAPPING (held down)**. A legend sits at the top right.

## 5. Restore, reset and flap damping

- Restore one link: pick it, then **🔧 Restore Lx**. Restore everything: **🔄 Restore All**.
- **Repeat the demo:** click **🛡️ Load Protected Demo** (it reloads the preset and resets all counters).
- **Flap damping (deliberate):** if the same link fails **3 times within 10 controller steps**, the 3rd restore holds it down for 5 steps instead of returning it. (A controller step is one simulation run, failure or restore.) The canvas shows "L3 FLAPPING (held down)", and the inspector explains it and shows the steps remaining. You can safely fail and restore L3 twice in a row; for more, reload the demo between runs.
- **🗑️ Clear** empties the network (after a confirmation). **Stop Simulation** only pauses automatic re-routing after edits; failures are still handled by the controller.

## 6. If something goes wrong

| Symptom | Fix |
|---|---|
| Toast "Cannot reach the NetRescue backend" | The server stopped. Start it with §1 step 2 and hard-refresh. |
| Numbers look unexpected | Click **🛡️ Load Protected Demo** (full reset of the demo). |
| L3 shows FLAPPING | It failed 3 times recently. Click **Load Protected Demo**, or let 5 controller steps pass (each simulation run, or failure or restore of another link, is one step; Stop Simulation is not). It then returns automatically. |
| Protection tab: "No current protection plan" or "not simulated yet" | Click **Start Simulation** (the demo button does this for you). |
| Protection tab: "Plan not in force" | More than one link is down. Click **Restore All**. |
| Port 8000 is busy or stuck | Run a second server on another port: `.venv312/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001`, then open http://127.0.0.1:8001/. The dashboard talks to whichever server served it. |
| Browser misbehaves | Open the page in a new private window; nothing important is stored in the browser. |
| Total failure | Show `qa/frontend-screenshots/journey1-01 … journey1-08` and `41-demo-protection-during-L3-failure.png`. |

## 7. Two-minute script

> **(0:00)** "When a link fails, most networks re-route *after* the fact by solving an optimization, while critical traffic waits. NetRescue plans ahead."
>
> **(0:15)** *Load Protected Demo.* "Two routes from A to E: a short 50 Mbps path and a long 200 Mbps path. There's a 10 Mbps emergency flow, a 40 Mbps essential flow, and lower-priority traffic."
>
> **(0:35)** *Protection tab.* "Our planner, a linear program, reserves bandwidth so the critical flows keep 10 and 40 Mbps under **any** single link failure. An independent checker re-verifies all 6 scenarios: PASS."
>
> **(0:55)** *Fail L3.* "I cut L3. Reflex is used: it switches to the precomputed reservations without running a solver, and the validator reports zero violations."
>
> **(1:10)** *Canvas and inspector.* "Emergency traffic still gets 10, essential 40. The short path is exactly full at 50 of 50; lower-priority traffic is what yields."
>
> **(1:25)** *Protection tab.* "And we're honest: the guarantee is in force now, but this plan doesn't cover a *second* failure. The dashboard says so."
>
> **(1:40)** *What-If or Sample Network (optional).* "What-If replays the real controller on a copy without touching the live network. And on a campus where every server is single-homed, we show the provable guarantee is zero rather than a misleading green check."
>
> **(1:55)** *Restore L3.* "Restore, and everything returns. Thank you."

## 8. Likely questions

- **Is recovery really 0.05 ms?** No. That is Reflex's computation time in this simulator. We model no detection, signalling or packets, and we make no zero-downtime claim.
- **What does "verified" mean?** An independent checker replays the baseline and every covered single-link failure, validates capacity and paths, and confirms each stated guarantee.
- **What happens on a second failure?** It is outside single-failure coverage, so the controller re-routes with PACAR and the Reactive LP, and the dashboard shows "Plan not in force".
- **Why 0 on the sample network?** Each critical flow there crosses a bridge link (e.g. SW1↔SRV1). One cut disconnects it, so the LP proves 0 is the best possible guarantee.
- **Is the LP globally optimal?** It is lexicographically optimal (P1 first, then P2, …, then latency) over the candidate paths, and the guarantees act as floors. PACAR and the Dijkstra baseline are heuristics.
- **How do you know it's correct?** 252 backend tests, plus randomized property testing against independently written reference optimizers and 50 scripted demo repetitions. On top of that, a real-browser suite checks that every displayed number equals the API response.
- **What is "flapping"?** Damping for unstable links: 3 failures within 10 controller steps hold the link down for 5 steps so routes don't oscillate.
