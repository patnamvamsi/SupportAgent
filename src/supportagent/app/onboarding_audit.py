"""Onboarding's audit trail (ADR-0017, WP-A6): every onboarding decision, in the event log.

Onboarding decides things a person must be able to trace: which plugin was
installed from where and how it did here, who approved which access, which
graph facts were confirmed, how readiness and calibration moved, what drift
was found. Each is one ``onboarding`` event (INV-03, append-only, encrypted
like every payload) on the tenant's ``onboarding`` trace, with
``payload.action`` saying which. :meth:`history` reads them back, newest first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.event import Event
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventRepository, EventSink
from supportagent.ports.idgen import IdGenerator

TRACE = "onboarding"
Action = Literal[
    "step",
    "plugin_installed",
    "plugin_disabled",
    "plugin_enabled",
    "access_decided",
    "graph_decided",
    "readiness_assessed",
    "calibration_fitted",
    "drift_found",
]


class OnboardingAudit:
    def __init__(
        self, *, events: EventSink, ids: IdGenerator, clock: Clock, tenant_id: str
    ) -> None:
        self._events = events
        self._ids = ids
        self._clock = clock
        self._tenant = tenant_id

    def record(
        self,
        action: Action,
        *,
        actor: str,
        detail: Mapping[str, object],
        status: Literal["ok", "error", "skipped"] = "ok",
    ) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        self._events.append(
            Event(
                id=event_id,
                tenant_id=self._tenant,
                trace_id=TRACE,
                span_id=f"onb_{event_id}",
                parent_span_id=None,
                kind=EventKind.ONBOARDING,
                actor=actor,
                status=status,
                payload={"v": 1, "action": action, **_no_floats(detail)},
                ts=self._clock.now(),
            )
        )


def history(store: EventRepository, tenant_id: str, *, limit: int = 30) -> list[dict[str, object]]:
    """The tenant's onboarding events, newest first, as the page shows them."""
    events = [e for e in store.trace_events(TRACE, tenant_id) if e.kind == EventKind.ONBOARDING]
    events.sort(key=lambda e: e.ts, reverse=True)
    return [
        {
            "at": e.ts.isoformat()[:16].replace("T", " "),
            "actor": e.actor,
            "status": e.status,
            **dict(e.payload),
        }
        for e in events[:limit]
    ]


def _no_floats(detail: Mapping[str, object]) -> dict[str, object]:
    """Event payloads are hashed and carry no floats (01 §7.1): shares become basis points."""
    out: dict[str, object] = {}
    for key, value in detail.items():
        if isinstance(value, float):
            out[f"{key}_bp"] = round(value * 10_000)
        elif isinstance(value, Mapping):
            out[key] = _no_floats(value)
        elif isinstance(value, list | tuple):
            out[key] = [_no_floats(v) if isinstance(v, Mapping) else v for v in value]
        else:
            out[key] = value
    return out


__all__ = ["TRACE", "Action", "OnboardingAudit", "history"]
