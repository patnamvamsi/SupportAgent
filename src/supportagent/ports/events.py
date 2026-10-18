"""Event log ports (01 §3).

``EventSink`` is the append side (01 §3): ``append`` writes inside the
caller's unit of work, so an event commits or rolls back with the state it
describes (10-wp WP-004, INV-03). The log is append-only at the database
level (trigger ``trg_events_append_only``); ``DELETE`` is reserved for the
retention sweep (03 §2.4).

``EventRepository`` is the read side (01 §3 ``UnitOfWork.events``): it
exposes intention-revealing methods only — replay fetches one trace's
decrypted events.
"""

from __future__ import annotations

from typing import Protocol

from supportagent.domain.model.event import Event


class EventSink(Protocol):
    """Append-only event log (01 §3)."""

    def append(self, event: Event) -> None:
        """Persist ``event`` in the current UoW transaction."""
        ...


class EventRepository(Protocol):
    """Read side of the event log (01 §3 ``UnitOfWork.events``)."""

    def trace_events(self, trace_id: str) -> list[Event]:
        """Decrypted events for one trace, ordered by ``(ts, span_id)``."""
        ...
