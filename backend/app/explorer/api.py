"""
Algorithm Explorer: HTTP endpoints
==================================
All endpoints are read-only with respect to the live network: they snapshot it
(under its lock) and trace the real controller code on private copies.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.explorer import service

router = APIRouter(prefix="/explorer", tags=["Algorithm Explorer"])


def _live_state():
    # Resolved at call time so a test (or tool) that swaps ``app.main.state`` is honoured.
    from app import main
    return main.state


class TraceRequest(BaseModel):
    algorithm: str
    failed_link: Optional[str] = None


class CompareRequest(BaseModel):
    failed_link: Optional[str] = None


def _session(session_id: str) -> service.ExplorerSession:
    try:
        return service.get_session(session_id)
    except service.SessionNotFound:
        raise HTTPException(status_code=404, detail="Explorer session not found or expired; capture the network again.")


@router.get("/algorithms")
def explorer_algorithms():
    """Algorithms the explorer can trace, with the honest trace level of each."""
    return [{"id": k, **v} for k, v in service.ALGORITHMS.items()]


@router.get("/live")
def explorer_live():
    """Fingerprint of the live network (to detect stale explorer snapshots) and the last live action."""
    return service.live_status(_live_state())


@router.post("/sessions")
def explorer_create_session():
    """Freeze the live network into an immutable explorer snapshot."""
    return service.create_session(_live_state()).summary()


@router.get("/sessions/{session_id}")
def explorer_get_session(session_id: str):
    return _session(session_id).summary()


@router.post("/sessions/{session_id}/trace")
def explorer_trace(session_id: str, req: TraceRequest):
    """Trace one algorithm on a private copy of the session snapshot."""
    session = _session(session_id)
    try:
        return session.trace(req.algorithm, req.failed_link or None)
    except service.ExplorerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/compare")
def explorer_compare(session_id: str, req: CompareRequest):
    """Run every routing algorithm on identical copies of the session snapshot."""
    session = _session(session_id)
    try:
        return session.compare(req.failed_link or None)
    except service.ExplorerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/live-action/trace")
def explorer_replay_last_action():
    """Replay the last live controller action from its pre-action snapshot, with tracing."""
    try:
        return service.replay_last_action(_live_state())
    except service.ExplorerError as e:
        raise HTTPException(status_code=404, detail=str(e))
