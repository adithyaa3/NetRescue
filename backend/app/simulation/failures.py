"""
Failure Handling Module
========================
Manages link failures, restoration, batch operations, and flapping detection.
Most logic is in state.py and engine.py; this module provides convenience functions.
"""

from __future__ import annotations
from app.state import SimulationState
from app.models import LinkStatus


def get_failure_summary(state: SimulationState) -> dict:
    """Get a summary of the current failure state."""
    failed = []
    flapping = []
    active = []
    
    for link in state.links.values():
        if link.status == LinkStatus.FAILED:
            failed.append({
                "id": link.id,
                "source": link.source,
                "target": link.target,
                "flap_count": link.flap_count,
            })
        elif link.status == LinkStatus.FLAPPING:
            flapping.append({
                "id": link.id,
                "source": link.source,
                "target": link.target,
                "flap_count": link.flap_count,
                "cooldown_remaining": max(
                    0,
                    state.flap_cooldown - (state.step - link.last_flap_step)
                ),
            })
        else:
            active.append({"id": link.id, "source": link.source, "target": link.target})

    return {
        "total_links": len(state.links),
        "active": len(active),
        "failed": len(failed),
        "flapping": len(flapping),
        "failed_links": failed,
        "flapping_links": flapping,
    }
