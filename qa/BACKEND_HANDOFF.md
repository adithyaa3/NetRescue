# Backend integration handoff

Backend release audit in progress. Codex owns backend Python; Claude owns frontend and static dashboard. No writes/tests against the user's live port 8000. Tests use isolated state/TestClient. Post frontend issues in `qa/FRONTEND_BACKEND_ISSUES.md`; I will read it periodically.

## Stable semantics

Shared physical capacity across both directions. Mbps bandwidth, ms latency. Synchronous recovery, no simulated elapsed-time detection/installation or packet model. No zero-downtime claim. P1/P2 are protected; optimizer stages P1 through P5, then latency. A verified zero guarantee is not positive protection. `normal_campus` has legitimate bridge failures forcing all critical guarantees to zero.

## Protected demonstration contract

1. `POST /topology/generate` JSON `{"preset":"congested_shortest"}` loads 5 nodes/5 links/3 demands. Response: `status`, `scenario`, `description`, `nodes`, `links`, `demands`, `pre_failed_links`.
2. `POST /demands` JSON `{"id":"P1_DEMO","source":"A","destination":"E","bandwidth":10,"category":"emergency"}` adds a 10 Mbps P1 flow. Returns a flat Demand object (`id`, `source`, `destination`, `bandwidth`, `priority`, `category`, `allocated_bandwidth`, `unserved_bandwidth`, `route`, `status`, `explanation`, `max_latency`, `tunnels`). Addition alone does not simulate.
3. `POST /simulate?algorithm=pacar` (no body) runs. Response: `version`, `step`, `algorithm`, `route_changes`, `computation_time_ms`, `violations`, `congestion_warnings`. Accepted modes: `pacar`, `baseline`, `lp`, `reactive_lp`.
4. `GET /protection/plan`: flat object: `active`, `plan_id`, `topology_version`, `demand_version`, `verified`, `covered_scenarios_count`, `solver_status`, `runtime_ms`, `critical_flows`, `verification_failures`. Each flow row: `flow_id`, `priority`, `requested_mbps`, `guaranteed_mbps`, `delivered_mbps`, `protection_pct`, `status`. Rows currently include noncritical flows, with zero guarantee. Inactive response includes `active:false`, `message`, empty `critical_flows`, zero scenario count. Expect D1=40 Mbps, P1_DEMO=10 Mbps, verified, six scenarios.
5. `POST /links/L3/fail`: flat result `version`, `step`, `event`, `link_id`, `reflex_used`, `reflex_time_ms`, `route_changes`, `violations`, `congestion_warnings`. Expect `reflex_used:true`, no violations. There is no `recovery_time` field; measured compute runtime is not simulated recovery time.
6. `GET /metrics`: `version`, `total_requested`, `total_delivered`, `total_unserved`, `critical_delivery_pct`, `general_delivery_pct`, `failed_links`, `disconnected_demands`, `active_demands`, `max_link_utilization`, `avg_link_utilization`, `avg_latency`, `route_changes`, `per_priority`, `link_utilizations`, `demand_details`. Link rows: `id`, `capacity`, `allocated_load`, `utilization`, `status`. Demand rows: `id`, `source`, `destination`, `category`, `priority`, `requested`, `allocated`, `unserved`, `status`, `route`. Expect D1=40, P1_DEMO=10, D2/D3=0, L1/L2 load50 each, critical delivery100% after L3 failure.
7. `POST /links/L3/restore` restores and reoptimizes. Returns simulation event result; flapping can deliberately exclude repeatedly toggled links.
8. Reset demo repeatably by reloading `/topology/generate` and repeating steps2–3. `POST /simulation/reset` clears all topology/traffic, returns `{"status":"ok"}`. Avoid Load Sample Network for positive-guarantee demo: it loads `normal_campus`.
9. `POST /protection/replan` returns the same summary as `GET /protection/plan`.

## Other actual API contracts

- `GET /topology`: `{nodes:[...],links:[...],version}`. `POST /topology` `{nodes:[...],links:[...]}`; `POST /topology/import` same plus `demands:[...]`.
- Node `POST /nodes`, `PUT/DELETE /nodes/{id}`; link `POST /links`, `PUT/DELETE /links/{id}`. Node fields `id,name,node_type,x,y,service_label`; link fields `id,source,target,capacity,latency`. No parallel links or self loops.
- Demand `GET /demands` returns `{demands:[...],version}`; `PUT/DELETE /demands/{id}`. `POST /demands/emergency` creates traffic and simulates; priority derives from `category`.
- `POST /failures/batch` `{link_ids:[...]}`. `GET /failures` summary. No restore-all endpoint currently: frontend must restore each unavailable link via its restore endpoint; repeated toggles may enter cooldown.
- `POST /analyze/what-if` `{link_ids:[...]}` returns a FLAT response: `failed_links`, `total_delivered_before`, `total_delivered_after`, `critical_delivered_before/after`, `general_delivered_before/after`, `unreachable_demands`, `congested_demands`, `alternative_routes`, `high_risk_links`, `congestion_warnings`, `explanation`, `before_metrics`, `after_metrics`. There is no `lost_bandwidth` or `disrupted_demands_count`. Derive throughput loss as before minus after in Mbps; if negative, show improvement. Disrupted count is the unique union of unreachable/congested IDs. Nested metrics are supplementary. Runs on snapshots, must not alter live state.
- `POST /compare` returns `{pacar:{...metrics...},reactive_lp:{...metrics...},baseline:{...metrics...}}`. Each includes `algorithm`, `computation_time_ms`, and metrics fields above.
- `GET /events?limit=100`: flat list of `{step,event_type,description,details}`, newest first. No `events` wrapper.
- `GET /analyze/vulnerabilities`: list of per-link vulnerability results.
- `GET /export`: `{topology:{nodes,links},demands,events,metrics,version}`; import topology+demands through `/topology/import`, extra export fields are not restored event history.
- `/health`, `/docs`, `/openapi.json` available. Errors use HTTP4xx/5xx with `detail`; non-2xx must not be treated as an assessment. Empty/missing fields are not zeros.

## Verification status

Historical values will be rerun now. Contract changes and discovered issues will be appended here immediately. No frontend edits by Codex.

## Release audit contract updates

- Invalid node/link edits now return HTTP400/422 before mutation. PUT node accepts only `name,node_type,service_label,x,y`; PUT link only `source,target,capacity,latency`. IDs and controller-owned status/load fields cannot be edited. Numeric input must be finite; capacity/bandwidth remain positive.
- Demand endpoint edits validate existing nodes. Explicit `max_latency:null` clears the limit; omission preserves it.
- Invalid bulk topology/import requests preserve the entire existing network instead of resetting it first.
- Empty `{}` generation now defaults to `normal_campus`. Unknown simulation algorithms return422.
- Unknown link IDs in What-If/batch failure now return400 atomically; duplicate IDs are deduplicated.
- Duplicate/missing-endpoint emergency requests return400 instead of an uncaught500.
- What-If `total_delivered_before` now measures the actual current allocation. The hypothetical after state still uses PACAR. Response field names remain unchanged.
- Repeating restore for an already-flapping link no longer restarts cooldown.
- All acceptance tests now use a private TestClient state; no test sends requests to port8000.

Current release retest: 192 passed, including isolated 25-step API acceptance. Further numerical/end-to-end checks in progress.

## Final backend behavior updates

- Cooldown expiry is processed by simulation steps, with a version advance. Metrics, export, failure summaries, comparison and analysis are read-only.
- Editing a link or demand clears routing for affected flows; deleting topology/traffic clears affected allocations and recomputes residual displayed loads. Unaffected flows remain installed. Run simulation after an edit to calculate new routes.
- Topology import restores topology/demand inputs, not previously computed link loads or routes. Run simulation after import. Export format is unchanged.
- `/demands/emergency` now always creates an emergency P1 flow, even when category is omitted. `/demands` continues to derive priority from the supplied category.
- Blank link/demand IDs are rejected. Nonfinite numerical JSON input returns a serializable422 validation error.
- Heuristic and LP allocation installation both validate before mutation. Protection `delivered_mbps` measures available-path delivery.

Backend final suite: 209 passed, including 40 new release regressions and isolated acceptance. Numerical canonical and 27 preset/algorithm runs passed. Repeated protected demo passes. No frontend issue file has been received as of the latest check. Browser testing remains Claude's responsibility.
