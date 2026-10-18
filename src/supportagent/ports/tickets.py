"""Ticket sink port (01 §7.5, ARCH §6.2): the external write surface.

The executor never talks to ServiceNow (or any ticketing system) directly —
it goes through this port. The concrete adapter (02 §5) speaks the real API;
the fake (``adapters.fakes.servicenow``) serves unit/property/invariant tests.

``TicketSink`` is *idempotent by construction* (01 §7.5): the sink compares
the current external state before writing, so re-applying an already-applied
action is a no-op. Reconciliation (INV-04) reads that same state back through
``find_marker`` — the last line of a journal entry carries the idempotency
marker ``[sa:<key>]``, so "already sent" is detectable from the outside.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from supportagent.domain.model.external import ExternalRef


class SnTable(StrEnum):
    """ServiceNow tables this product writes to (01 §7.5)."""

    INCIDENT = "incident"
    EM_ALERT = "em_alert"


class JournalField(StrEnum):
    """Append-only journal columns (01 §7.5)."""

    WORK_NOTES = "work_notes"
    COMMENTS = "comments"


class TicketSink(Protocol):
    """One external ticketing system (01 §7.5). All writes idempotent."""

    def append_journal(
        self,
        table: SnTable,
        sys_id: str,
        field: JournalField,
        text: str,
    ) -> ExternalRef:
        """Append ``text`` as the last entry of ``field`` on ``sys_id``.

        ``text`` already carries the ``[sa:<key>]`` marker as its last line
        (the handler appends it); the sink stores it verbatim.
        """
        ...

    def update_fields(
        self,
        table: SnTable,
        sys_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        """Set ``fields`` on ``sys_id`` (read-compare first: a no-op if equal)."""
        ...

    def find_marker(
        self,
        table: SnTable,
        sys_id: str,
        marker: str,
    ) -> ExternalRef | None:
        """Reconciliation read (INV-04): the entry carrying ``marker``, if any.

        Returns the ``ExternalRef`` of the entry that already carries the
        marker (so ``mark_done`` can record it), or ``None`` if the action was
        never applied externally.
        """
        ...


__all__ = ["JournalField", "SnTable", "TicketSink"]
