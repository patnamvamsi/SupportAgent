"""Inbound handler port (01 §3, 02 §6): the sync service's consumer.

The sync service (``app.sync``) polls a ServiceNow table, dedups, filters
self-echoes, and hands the result to an ``InboundHandler`` — a duck-typed
consumer the application wires up. New records become ``AlertEvent``s
(``on_alert``); changes to a known record are delivered as ``ChangeRecord``s
(``on_changes``). The handler never sees a vendor type (01 §3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.external import ExternalCaseRef
from supportagent.domain.model.ticket import ChangeRecord


class InboundHandler(Protocol):
    """Consumes inbound ticket activity (02 §6)."""

    def on_alert(self, evt: AlertEvent) -> None:
        """A new record appeared (or a record we have not seen before)."""
        ...

    def on_changes(self, case_ref: ExternalCaseRef, changes: Sequence[ChangeRecord]) -> None:
        """A known record changed; ``changes`` are its new audit/journal entries."""
        ...


__all__ = ["InboundHandler"]
