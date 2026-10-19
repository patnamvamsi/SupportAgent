"""Ticket ports (01 §3, 02 §5-§6): the external read and write surface.

The application never talks to ServiceNow (or any ticketing system) directly —
it goes through these ports. The concrete adapter (``adapters.servicenow``)
speaks the real API; the fake (``adapters.fakes.servicenow``) serves
unit/property/contract tests.

``TicketSource`` (02 §6) is the read side: ``poll`` returns the records
changed since a cursor, ``history`` returns the audit/journal changes for one
record. ``TicketSink`` (01 §7.5) is the write side.

``TicketSink`` is *idempotent by construction* (01 §7.5): the sink compares
the current external state before writing, so re-applying an already-applied
action is a no-op. Reconciliation (INV-04) reads that same state back through
``find_marker`` — the last line of a journal entry carries the idempotency
marker ``[sa:<key>]``, so "already sent" is detectable from the outside.

No method returns a vendor type: the adapter maps raw JSON into the domain
``TicketRecord`` / ``ChangeRecord`` (01 §3, 02 §6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord


class SnTable(StrEnum):
    """ServiceNow tables this product writes to (01 §7.5)."""

    INCIDENT = "incident"
    EM_ALERT = "em_alert"


class JournalField(StrEnum):
    """Append-only journal columns (01 §7.5)."""

    WORK_NOTES = "work_notes"
    COMMENTS = "comments"


@dataclass(frozen=True)
class PollBatch:
    """One page of ``TicketSource.poll`` (02 §6).

    ``records`` are the current state of each changed record in the window,
    ordered by ``(sys_updated_on, sys_id)``. ``next_cursor`` is the maximum
    ``sys_updated_on`` seen in the batch — the caller advances its per-table
    cursor to it (ARCH §8.1). A full page means more may follow: the caller
    fetches the next page with ``offset += limit``.
    """

    records: Sequence[TicketRecord]
    next_cursor: datetime


class TicketSource(Protocol):
    """One external ticketing system's read surface (02 §6)."""

    def poll(self, table: SnTable, since: datetime, limit: int, offset: int = 0) -> PollBatch:
        """Records on ``table`` updated at or after ``since`` (one page).

        ``limit`` is the page size and ``offset`` the number of matching
        records to skip (02 §6 ``sysparm_offset``). The result is ordered by
        ``(sys_updated_on, sys_id)``, so paging by offset from a fixed
        ``since`` reaches every record even when more than ``limit`` share
        one second. ``next_cursor`` is the page's max ``sys_updated_on``.
        """
        ...

    def history(
        self,
        table: SnTable,
        sys_id: str,
        after_mod_count: int,
        since: datetime | None = None,
    ) -> Sequence[ChangeRecord]:
        """Audit + journal changes for ``sys_id`` (02 §6 ``history``).

        Audit entries newer than ``after_mod_count`` (by
        ``record_checkpoint``) plus the journal entries created at or after
        ``since`` (all of them when ``None``), ordered by ``ts``.
        """
        ...


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


__all__ = ["JournalField", "PollBatch", "SnTable", "TicketSink", "TicketSource"]
