"""
Algorithm Explorer: trace recording
===================================
Optional, passive instrumentation for the routing and recovery algorithms.

Every instrumented function accepts ``trace=None``. When a ``TraceRecorder`` is
supplied, the function reports what it is doing (inputs, candidates, decisions,
intermediate state) through ``emit``. Nothing is ever read back from the
recorder, so tracing cannot influence a routing decision; with ``trace=None``
the instrumented code paths are skipped entirely.

``emit`` converts its arguments into JSON-safe copies at call time (later
mutations of the algorithm's working dictionaries do not alter what was
recorded) and never raises: a recording problem is stored in ``errors``.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Optional

DEFAULT_MAX_EVENTS = 20000


def json_safe(value: Any) -> Any:
    """Deep, JSON-serialisable copy. Non-finite floats become None."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((json_safe(v) for v in value), key=lambda v: str(v))
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    if hasattr(value, "item"):  # NumPy scalars
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "tolist"):  # NumPy arrays
        try:
            return json_safe(value.tolist())
        except Exception:
            pass
    return str(value)


class TraceRecorder:
    """Collects raw algorithm events in execution order."""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS):
        self.events: list[dict] = []
        self.max_events = max_events
        self.truncated = False
        self.errors: list[str] = []
        self.phase: Optional[str] = None

    def set_phase(self, phase: Optional[str]) -> None:
        """Label the events that follow (e.g. "reflex", "reactive_lp")."""
        self.phase = phase

    def emit(self, kind: str, **data: Any) -> None:
        try:
            if len(self.events) >= self.max_events:
                self.truncated = True
                return
            self.events.append({
                "seq": len(self.events),
                "kind": kind,
                "phase": self.phase,
                "data": json_safe(data),
            })
        except Exception as exc:  # recording must never disturb the algorithm
            try:
                self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")
            except Exception:
                pass

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]
