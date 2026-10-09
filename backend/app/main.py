"""
NetRescue — FastAPI Backend
============================
REST API for the network resilience simulation platform.
All endpoints documented via FastAPI Swagger at /docs.
"""

from __future__ import annotations
import json
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.models import (
    Node, Link, Demand, NodeCreate, LinkCreate, DemandCreate, DemandUpdate,
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
from app.analysis.metrics import compute_metrics
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
            state.reset()
            for n in data.nodes:
                state.add_node(n)
            for l in data.links:
                state.add_link(l)
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
        n = Node(**node.model_dump())
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
def update_node(node_id: str, update: dict):
    try:
        with state.lock():
            node = state.update_node(node_id, **update)
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
def update_link(link_id: str, update: dict):
    try:
        with state.lock():
            link = state.update_link(link_id, **update)
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
        )
        with state.lock():
            state.add_demand(d)
        return d.model_dump()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/demands/{demand_id}", tags=["Demands"])
def update_demand(demand_id: str, update: DemandUpdate):
    try:
        with state.lock():
            d = state.update_demand(
                demand_id,
                bandwidth=update.bandwidth,
                category=update.category,
                source=update.source,
                destination=update.destination,
            )
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


# ── Simulation ────────────────────────────────────────────────────────

@app.post("/simulate", tags=["Simulation"])
def run_simulation(algorithm: str = Query("pacar", enum=["pacar", "baseline"])):
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
    result = handle_batch_failures(state, req.link_ids)
    return result


@app.get("/failures", tags=["Failures"])
def get_failures():
    with state.lock():
        return get_failure_summary(state)


# ── Emergency ─────────────────────────────────────────────────────────

@app.post("/demands/emergency", tags=["Demands"])
def inject_emergency(demand: DemandCreate):
    """Add an emergency demand and trigger preemption via re-simulation."""
    result = add_emergency_demand(state, demand)
    return result


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
    with state.lock():
        return run_what_if(state, req.link_ids)


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
