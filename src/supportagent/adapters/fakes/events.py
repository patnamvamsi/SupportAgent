"""RecordingEventSink test double (04 §3): captures ``EventSink.append``.

Unit-level fake (04 §3): it captures appended events in order and offers the
``.kinds()`` helper — no database, so app-layer unit tests assert on the
event stream without wiring the SQL repo.
"""

from __future__ import annotations

from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event


class RecordingEventSink:
    """Captures ``EventSink.append`` into a list (04 §3)."""

    def __init__(self) -> None:
        self.appended: list[Event] = []

    def append(self, event: Event) -> None:
        self.appended.append(event)

    def kinds(self) -> list[EventKind]:
        """The ``EventKind`` of each appended event, in order (04 §3)."""
        return [event.kind for event in self.appended]
