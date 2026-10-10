"""
NetRescue — FastAPI Backend
============================
REST API for the network resilience simulation platform.
All endpoints documented via FastAPI Swagger at /docs.
"""

from __future__ import annotations
import json
from typing import Optional, Literal
import math

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.models import (
    Node, Link, Demand, NodeCreate, LinkCreate, DemandCreate, DemandUpdate, NodeUpdate, LinkUpdate,
    TopologyData, TopologyImport, FailureRequest, WhatIfRequest,
    GenerateTopologyRequest, TrafficCategory, CATEGORY_TO_PRIORITY,
    DemandStatus, LinkStatus, ExportData,
)
from app.state import state
from app.simulation.engine import (
    simulate, handle_link_failure, handle_link_restore,
    handle_batch_failures, add_emergency_demand,
    reset_simulation, compare_algorithms,
)
from app.simulation.events import get_events
from app.simulation.failures import get_failure_summary
from app.analysis.metrics import compute_metrics, delivered_bandwidth
from app.analysis.vulnerability import rank_vulnerabilities
from app.analysis.what_if import run_what_if
from app.scenarios.presets import get_scenario_list, load_scenario, SCENARIOS

app = FastAPI(
    title="NetRescue API",
    description=(
        "Intelligent Network Resilience & Disaster Recovery Platform. "
        "This is a bandwidth-demand network simulator, not a real packet-forwarding router."
    ),
    version="1.0.0",
)

import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def invalid_request_handler(request, exc):
    # The JSON parser permits NaN/Infinity; validation rejects them, but error
    # payloads must also be valid JSON rather than crashing the 422 serializer.
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {k: safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(v) for v in value]
        return value
    return JSONResponse(status_code=422, content={"detail": safe(jsonable_encoder(exc.errors()))})


# ── Web UI & Health ──────────────────────────────────────────────────

@app.get("/", tags=["System"])
def root_ui():
    """Serve the NetRescue Interactive Dashboard."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"status": "ok", "message": "NetRescue API Running. See /docs for Swagger UI."}


@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": state.version, "step": state.step}


# ── Topology ──────────────────────────────────────────────────────────

@app.get("/topology", tags=["Topology"])
def get_topology():
    with state.lock():
        return {
            "nodes": [n.model_dump() for n in state.nodes.values()],
            "links": [l.model_dump() for l in state.links.values()],
            "version": state.version,
        }


@app.post("/topology", tags=["Topology"])
def set_topology(data: TopologyData):
    try:
        with state.lock():
            state.load_topology(data.nodes, data.links)
        return {"status": "ok", "nodes": len(data.nodes), "links": len(data.links)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/topology/import", tags=["Topology"])
def import_topology(data: TopologyImport):
    try:
        with state.lock():
            state.load_topology(data.nodes, data.links, data.demands)
        return {
            "status": "ok",
            "nodes": len(data.nodes),
            "links": len(data.links),
            "demands": len(data.demands),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/topology/generate", tags=["Topology"])
def generate_topology(req: GenerateTopologyRequest):
    try:
        scenario = load_scenario(req.preset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    with state.lock():
        state.load_topology(
            scenario["nodes"],
            scenario["links"],
            scenario.get("demands", []),
        )
        state.add_event(
            "scenario_loaded",
            f"Loaded scenario: {scenario['name']}",
            {"scenario": req.preset, "description": scenario["description"]},
        )
    
    # Handle pre-configured failures
    fail_links = scenario.get("fail_links", [])
    if fail_links:
        handle_batch_failures(state, fail_links)
    
    return {
        "status": "ok",
        "scenario": scenario["name"],
        "description": scenario["description"],
        "nodes": len(scenario["nodes"]),
        "links": len(scenario["links"]),
        "demands": len(scenario.get("demands", [])),
        "pre_failed_links": fail_links,
    }


@app.get("/scenarios", tags=["Topology"])
def list_scenarios():
    return get_scenario_list()


# ── Nodes ─────────────────────────────────────────────────────────────

@app.post("/nodes", tags=["Nodes"])
def add_node(node: NodeCreate):
    try:
        node_data = node.model_dump()
        node_data["id"] = node_data["id"].strip()
        if not node_data["id"]:
            raise ValueError("Node ID cannot be empty")
        if not node_data.get("name") or not node_data["name"].strip():
            node_data["name"] = node_data["id"]
        n = Node(**node_data)
        with state.lock():
            state.add_node(n)
        return n.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/nodes/{node_id}", tags=["Nodes"])
def delete_node(node_id: str):
    try:
        with state.lock():
            state.remove_node(node_id)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/nodes/{node_id}", tags=["Nodes"])
def update_node(node_id: str, update: NodeUpdate):
    try:
        with state.lock():
            node = state.update_node(node_id, **update.model_dump(exclude_unset=True))
        return node.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Links ─────────────────────────────────────────────────────────────

@app.post("/links", tags=["Links"])
def add_link(link: LinkCreate):
    try:
        l = Link(**link.model_dump())
        with state.lock():
            state.add_link(l)
        return l.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/links/{link_id}", tags=["Links"])
def delete_link(link_id: str):
    try:
        with state.lock():
            state.remove_link(link_id)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put("/links/{link_id}", tags=["Links"])
def update_link(link_id: str, update: LinkUpdate):
    try:
        with state.lock():
            link = state.update_link(link_id, **update.model_dump(exclude_unset=True))
        return link.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/links/{link_id}/fail", tags=["Failures"])
def fail_link(link_id: str):
    try:
        result = handle_link_failure(state, link_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/links/{link_id}/restore", tags=["Failures"])
def restore_link(link_id: str):
    try:
        result = handle_link_restore(state, link_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Demands ───────────────────────────────────────────────────────────

@app.get("/demands", tags=["Demands"])
def get_demands():
    with state.lock():
        return {
            "demands": [d.model_dump() for d in state.demands.values()],
            "version": state.version,
        }


@app.post("/demands", tags=["Demands"])
def add_demand(demand: DemandCreate):
    try:
        d = Demand(
            id=demand.id,
            source=demand.source,
            destination=demand.destination,
            bandwidth=demand.bandwidth,
            category=demand.category,
            priority=CATEGORY_TO_PRIORITY[demand.category],
            max_latency=demand.max_latency,
        )
        with state.lock():
            stored = state.add_demand(d)
        return stored.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/demands/{demand_id}", tags=["Demands"])
def update_demand(demand_id: str, update: DemandUpdate):
    try:
        with state.lock():
            d = state.update_demand(demand_id, **update.model_dump(exclude_unset=True))
        return d.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/demands/{demand_id}", tags=["Demands"])
def delete_demand(demand_id: str):
    try:
        with state.lock():
            state.remove_demand(demand_id)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Protection ─────────────────────────────────────────────────────────

@app.get("/protection/plan", tags=["Protection"])
def get_protection_plan():
    """Current protection plan and how it relates to the live network.

    Rows: ``planned`` is False for P3-P5 traffic, which the planner deliberately
    excludes (status "Not planned"); a planned P1/P2 row with ``status: "None"``
    has a verified guarantee of exactly 0 Mbps.

    Plan state: ``base_unavailable_links`` were already down when it was computed;
    ``current_scenario`` is the covered scenario the live network is in (None when
    the live failures exceed single-failure coverage); ``in_force`` means the plan is
    verified, covers the live network and every planned flow is routed (otherwise
    ``not_in_force_reason`` is "unverified", "uncovered_failures",
    "pending_traffic" or "guarantee_not_met"); ``further_failures_covered`` means one more single-link
    failure is still covered.
    """
    with state.lock():
        plan = state.protection_plan
        if not plan:
            has_critical = any(d.priority.value <= 2 for d in state.demands.values())
            return {
                "active": False,
                "message": ("No current protection plan: the network or traffic changed since the last "
                            "simulation. Run the simulation (or Recompute Plan) to plan protection."
                            if has_critical else
                            "No active protection plan (requires P1 or P2 critical traffic and usable topology)"),
                "critical_flows": [],
                "covered_scenarios_count": 0,
            }
        flows = []
        for fid, g in plan.per_flow_guaranteed.items():
            d = state.demands.get(fid)
            req = d.bandwidth if d else 0.0
            deliv = delivered_bandwidth(state, d) if d else 0.0
            p = d.priority.value if (d and hasattr(d.priority, "value")) else 1
            planned = p <= 2
            if planned:
                pct = round((g / req * 100) if req > 0 else 100.0, 1)
                status_label = "Full" if g >= req - 1e-4 else ("Partial" if g > 0 else "None")
            else:
                pct = None
                status_label = "Not planned"
            flows.append({
                "flow_id": fid,
                "priority": p,
                "planned": planned,
                "requested_mbps": req,
                "guaranteed_mbps": round(g, 6),
                "delivered_mbps": round(deliv, 6),
                "protection_pct": pct,
                "status": status_label,
            })
        unavailable = sorted(lid for lid in state.links if not state.is_link_usable(lid))
        scenario = plan.scenario_for(unavailable)
        pending = sorted(r["flow_id"] for r in flows
                         if r["planned"] and r["flow_id"] in state.demands
                         and state.demands[r["flow_id"]].status == DemandStatus.PENDING)
        unmet = sorted(r["flow_id"] for r in flows
                       if r["planned"] and r["flow_id"] not in pending
                       and r["delivered_mbps"] < r["guaranteed_mbps"] - 1e-4)
        if not plan.verified:
            reason = "unverified"
        elif scenario is None:
            reason = "uncovered_failures"
        elif pending:
            reason = "pending_traffic"
        elif unmet:
            reason = "guarantee_not_met"
        else:
            reason = None
        in_force = reason is None
        return {
            "active": True,
            "plan_id": plan.plan_id,
            "topology_version": plan.topology_version,
            "demand_version": plan.demand_version,
            "verified": plan.verified,
            "covered_scenarios_count": len(plan.covered_scenarios),
            "solver_status": plan.solver_status,
            "runtime_ms": round(plan.runtime_ms, 2),
            "critical_flows": flows,
            "verification_failures": plan.verification_failures,
            "base_unavailable_links": list(plan.base_unavailable),
            "live_unavailable_links": unavailable,
            "current_scenario": scenario,
            "in_force": in_force,
            "not_in_force_reason": reason,
            "pending_flows": pending,
            "unmet_flows": unmet,
            "further_failures_covered": in_force and scenario == "no_failure",
        }


@app.post("/protection/replan", tags=["Protection"])
def trigger_protection_replan():
    with state.lock():
        from app.simulation.engine import (
            update_simulation_protection_plan, refresh_candidate_tunnels, enforce_plan_guarantees)
        # Candidate paths must reflect the current demands and topology (a demand
        # added since the last simulation would otherwise have no tunnels to reserve).
        refresh_candidate_tunnels(state)
        update_simulation_protection_plan(state)
        # A new plan is only shown as in force if the installed allocation honours it.
        # Unrouted (pending) demands are left for the next simulation.
        if not any(d.status == DemandStatus.PENDING for d in state.demands.values()):
            enforce_plan_guarantees(state)
        return get_protection_plan()


# ── Simulation ────────────────────────────────────────────────────────

@app.post("/simulate", tags=["Simulation"])
def run_simulation(algorithm: Literal["pacar", "baseline", "reactive_lp", "lp"] = Query("pacar")):
    result = simulate(state, algorithm=algorithm)
    return result


@app.post("/simulation/step", tags=["Simulation"])
def simulation_step():
    result = simulate(state)
    return result


@app.post("/simulation/reset", tags=["Simulation"])
def simulation_reset():
    with state.lock():
        state.reset()
    return {"status": "ok"}


@app.post("/failures/batch", tags=["Failures"])
def batch_failures(req: FailureRequest):
    try:
        return handle_batch_failures(state, req.link_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/failures", tags=["Failures"])
def get_failures():
    with state.lock():
        return get_failure_summary(state)


# ── Emergency ─────────────────────────────────────────────────────────

@app.post("/demands/emergency", tags=["Demands"])
def inject_emergency(demand: DemandCreate):
    """Add an emergency demand and trigger preemption via re-simulation."""
    try:
        return add_emergency_demand(state, demand)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Metrics ───────────────────────────────────────────────────────────

@app.get("/metrics", tags=["Metrics"])
def get_metrics():
    with state.lock():
        return compute_metrics(state)


# ── Events ────────────────────────────────────────────────────────────

@app.get("/events", tags=["Events"])
def events(limit: int = Query(100, ge=1, le=1000)):
    with state.lock():
        return get_events(state, limit)


# ── Comparison ────────────────────────────────────────────────────────

@app.post("/compare", tags=["Analysis"])
def compare():
    with state.lock():
        return compare_algorithms(state)


# ── What-if ───────────────────────────────────────────────────────────

@app.post("/analyze/what-if", tags=["Analysis"])
def what_if(req: WhatIfRequest):
    try:
        with state.lock():
            return run_what_if(state, req.link_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Vulnerability ─────────────────────────────────────────────────────

@app.get("/analyze/vulnerabilities", tags=["Analysis"])
def vulnerabilities():
    with state.lock():
        return rank_vulnerabilities(state)


# ── Export ─────────────────────────────────────────────────────────────

@app.get("/export", tags=["Export"])
def export_data():
    with state.lock():
        metrics = compute_metrics(state)
        return {
            "topology": {
                "nodes": [n.model_dump() for n in state.nodes.values()],
                "links": [l.model_dump() for l in state.links.values()],
            },
            "demands": [d.model_dump() for d in state.demands.values()],
            "events": [
                {
                    "step": e.step,
                    "event_type": e.event_type,
                    "description": e.description,
                    "details": e.details,
                }
                for e in state.events
            ],
            "metrics": metrics,
            "version": state.version,
        }
