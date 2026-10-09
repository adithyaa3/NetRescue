"""
Event History Module
=====================
Manages simulation event timeline and explanations.
"""

from __future__ import annotations
from app.state import SimulationState
from app.models import SimulationEvent


def get_events(state: SimulationState, limit: int = 100) -> list[dict]:
    """Get recent events, newest first."""
    events = state.events[-limit:]
    return [
        {
            "step": e.step,
            "event_type": e.event_type,
            "description": e.description,
            "details": e.details,
        }
        for e in reversed(events)
    ]


def generate_route_change_explanation(
    demand_id: str,
    old_route: list[str] | None,
    new_route: list[str] | None,
    reason: str,
) -> str:
    """Generate a human-readable explanation for a route change."""
    old_str = " → ".join(old_route) if old_route else "none"
    new_str = " → ".join(new_route) if new_route else "none"
    return f"Flow {demand_id} moved from [{old_str}] to [{new_str}] because {reason}."
