# Default sample protection investigation

## Verdict

The default sample's zero guarantees are mathematically unavoidable under the current promise covering every single physical-link failure. No Planner bug causing these zeros was found. No production code, failure coverage, guarantee definition, capacity model or objective was changed in this investigation. All runs used isolated in-memory states; the user's live API network was not touched. Pre-existing uncommitted work, including the interrupted What-If fix, was preserved.

## Exact reproduction and numbers

`normal_campus`, the preset selected by the dashboard's Load Sample Network handler, has 10 nodes, 11 links and 8 demands. Running the actual `simulate` pipeline returns:

- Planner status: `optimal`.
- Covered scenarios: `no_failure`, `fail_L1` through `fail_L11` (12 total).
- Guarantee Checker: 12/12 passed, no failures.
- Guarantees D1–D8: exactly 0 Mbps, not a rounding effect.
- Reservations for every flow: empty after reservation minimization.
- Currently delivered critical bandwidth in the healthy sample: D1 30, D4 20, D6 10, D7 5 Mbps (65 Mbps total).

Current delivered traffic and a worst-case guarantee are different quantities. The sample serves its critical demands while healthy; it cannot guarantee those rates through every covered failure.

## Mathematical proof

For flow f, the LP requires for each covered scenario q:

```
0 <= b[f] <= demand[f]
sum(a[f,t] for surviving tunnels t in q) >= b[f]
```

If failure q disconnects the endpoints, there is no surviving tunnel. The empty sum is zero, so `0 >= b[f]`. Together with `b[f] >= 0`, this forces `b[f] = 0` regardless of capacities, solver choices, priorities or candidate count.

Independent graph edge removal and exhaustive simple-path enumeration establish:

| Flow | Priority | Endpoints | Demand | All failure links that disconnect this flow | Generated / exhaustive paths | Guarantee |
|---|---|---|---:|---|---:|---:|
| D1 | P2 | EP1 → SRV1 | 30 Mbps | L8, L9 | 1 / 1 | 0 Mbps |
| D4 | P2 | SRV1 → GW1 | 20 Mbps | L4, L8 | 3 / 3 | 0 Mbps |
| D6 | P1 | SRV3 → GW1 | 10 Mbps | L7, L11 | 3 / 3 | 0 Mbps |
| D7 | P1 | EP1 → SRV3 | 5 Mbps | L4, L7, L9, L11 | 3 / 3 | 0 Mbps |

Relevant physical links:

- L4: R1 ↔ SW1, 500 Mbps. This is SW1's only connection to the core.
- L7: R2 ↔ SW3, 500 Mbps. This is SW3's only connection to the core.
- L8: SW1 ↔ SRV1, 200 Mbps. SRV1 is attached only here.
- L9: SW1 ↔ EP1, 100 Mbps. EP1 is attached only here.
- L11: SW3 ↔ SRV3, 200 Mbps. SRV3 is attached only here.

D1 remains connected if L4 fails because both endpoints are inside the SW1 branch, but L8 or L9 still forces its guarantee to zero. D6 and D7 have three alternatives inside the core; every one must traverse L7 and L11. Core redundancy cannot repair their single attachment to the core/server.

D2, D3, D5 and D8 have priorities P3, P4, P3 and P5. The Planner deliberately optimizes guarantees only for P1/P2 and reports zero for other priorities. Their zeros are not evidence of a critical-flow optimization defect. In particular, D8's core endpoints are redundant; its zero is the priority-policy convention.

## Coverage, candidates and LP implementation

All 11 physical links genuinely exist. Each declared single-link failure is a legitimate event; a failure that partitions the network is difficult to protect, not an impossible event to omit. No access-link or bridge failure was excluded.

There are no missing critical-flow alternatives in this sample: generated candidate paths exactly equal independently enumerated simple paths for all four critical flows. Re-solving with every simple path for every flow still produces all-zero guarantees and passes all 12 checks. No latency limit prunes paths in this preset.

The actual LP has nonnegative reservations, demand-bounded guarantees, shared physical-link reservation capacity constraints, and survival rows of the form `b - sum(surviving a) <= 0`. Objectives are the existing lexicographic P1 maximum, then P2 maximum, then reservation minimization with individual guarantees locked. Their signs and locking behavior match the repository's intended model. Both priority maxima are necessarily zero here; reservation minimization therefore chooses zero reservations. An aggregate critical objective or independent full-duplex capacities would give the same zero result for these disconnected cases.

`OPTIMAL` means the maximum achievable objective was found, even when that maximum is zero. `100% PASS` means all stated guarantees were verified; it does not mean all demands are protected at 100%. A plan promising zero can pass. Positive guarantees and the Protected % column must be examined before claiming positive protection. The dashboard's generic positive verification wording should not be interpreted as a positive bandwidth SLA.

## Verified positive demonstration

Use the existing `congested_shortest` preset (also used by `pacar_vs_dijkstra`): five nodes, five links, three original flows.

Its critical P2 flow D1 demands 40 Mbps from A to E, with two physically disjoint paths:

- A–B–E: L1, L2, each 50 Mbps; total latency 10 ms.
- A–C–D–E: L3, L4, L5, each 200 Mbps; total latency 30 ms.

Actual result: D1 guarantee 40 Mbps; reserve 40 Mbps on each path; Planner optimal; baseline plus all five physical-link failures verified (6/6). Direct Reflex checks and controller failures of L1 and L3 preserve D1's 40 Mbps.

For a P1 demonstration, add a separate A → E emergency flow `P1_DEMO`, demand 10 Mbps. This is an additional demo demand on an existing topology, not an architecture or failure-model change.

Actual verified result:

| Flow | Priority | Guarantee | Short-path reservation | Long-path reservation |
|---|---|---:|---:|---:|
| P1_DEMO | P1 | 10 Mbps | 10 Mbps | 10 Mbps |
| D1 | P2 | 40 Mbps | 40 Mbps | 40 Mbps |

All six scenarios pass. Each path reserves 50 Mbps total, within its capacity. This also reaches the upper bound: failure of any long-path link leaves only the 50 Mbps short path, so combined critical protection cannot exceed 50 Mbps.

Fail L3 (A–C) for the strongest demonstration. Actual controller output uses Reflex with zero validation violations. Afterwards D1 receives 40 Mbps, P1_DEMO receives 10 Mbps, D2 and D3 receive zero, and L1/L2 each carry exactly 50 Mbps (100% utilization). The critical guarantees are maintained through a genuinely capacity-limited failure.

## Exact live demonstration steps

The following commands intentionally replace the currently loaded network with the demo preset. They were not run against the user's live network during investigation. Use the local API on port 8000; start it first if needed:

```sh
cd /Users/adharshsundaramurthy/Desktop/Commitcon/The-Internet-Goes-Down
source .venv312/bin/activate
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

With that server running, use a second terminal:

```sh
curl --fail -sS -X POST http://127.0.0.1:8000/topology/generate \
  -H 'Content-Type: application/json' \
  -d '{"preset":"congested_shortest"}'

curl --fail -sS -X POST http://127.0.0.1:8000/demands \
  -H 'Content-Type: application/json' \
  -d '{"id":"P1_DEMO","source":"A","destination":"E","bandwidth":10,"category":"emergency"}'

curl --fail -sS -X POST 'http://127.0.0.1:8000/simulate?algorithm=pacar'
curl --fail -sS http://127.0.0.1:8000/protection/plan
```

Open/refresh `http://127.0.0.1:8000/` and open Protection Guarantees. Confirm P1_DEMO = 10 Mbps, D1 = 40 Mbps, six scenarios and verified status. Avoid Load Sample Network, which would reload `normal_campus`.

Trigger the failure with the dashboard's link-failure control or:

```sh
curl --fail -sS -X POST http://127.0.0.1:8000/links/L3/fail
curl --fail -sS http://127.0.0.1:8000/metrics
```

Confirm `reflex_used: true`, P1_DEMO delivered 10 Mbps, D1 delivered 40 Mbps, and L1/L2 load 50 Mbps each. Refresh the dashboard as needed. For the original preset without an added P1 flow, omit the `/demands` command; D1 alone retains a 40 Mbps verified guarantee.

To return to the original sample, click Load Sample Network and Start Simulation. Its all-zero worst-case guarantees remain correct.

## Verification and files added

Added ten test cases in `backend/tests/test_sample_protection.py`: independent disconnect sets, exhaustive candidate equivalence, zero optimum, rejection of a fabricated positive claim, and positive P1/P2 recovery across all declared failures plus controller L1/L3 failures.

Command:

```sh
PYTHONPATH=backend .venv312/bin/python -m pytest backend/tests \
  --ignore=backend/tests/test_acceptance_25steps.py -q --tb=short
```

Actual result: `168 passed in 1.56s`. The live acceptance test was deliberately excluded because it resets the API network on port 8000. All investigation tests use isolated state. Existing assertions were preserved; `git diff --check` passes.

Reproduce the numerical investigation without changing the live network:

```sh
PYTHONPATH=backend .venv312/bin/python qa/investigate_sample_protection.py
```

Other added files: `qa/investigate_sample_protection.py`, `qa/sample-protection-results.json`, `qa/sample-protection-tests.log`, and this `qa/PROTECTION_FINDINGS.md`. No production files were changed in this investigation. No commits, pushes, pulls, git resets, branch switches or architecture changes occurred.
