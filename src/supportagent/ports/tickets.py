"""Ticket ports (01 §3, 02 §5-§6; WP-M07): the connector contract.

The application never talks to an incident system directly: it goes through
a **connector**, which is a ``TicketSource`` (read), a ``TicketSink``
(write), a :class:`~supportagent.domain.connector.ConnectorManifest` (what
it can do) and its :class:`~supportagent.domain.connector.TicketSemantics`
(how its states and priorities map to the case lifecycle and severity).
ServiceNow (``adapters.servicenow``) is the first connector; the fake
(``adapters.fakes.servicenow``) serves the unit, property and contract tests.

``TicketSource`` (02 §6) is the read side: ``poll`` returns the records
changed since a cursor, ``history`` returns the field and note changes for
one record. ``TicketSink`` (01 §7.5) is the write side.

``TicketSink`` is *idempotent by construction* (01 §7.5): the sink compares
the current external state before writing, so re-applying an already-applied
action is a no-op. Reconciliation (INV-04) reads that same state back through
``find_marker``: the last line of a note carries the idempotency marker
``[sa:<key>]``, so "already sent" is detectable from the outside.

No method returns a vendor type: the connector maps its raw records into the
domain ``TicketRecord`` / ``ChangeRecord`` (01 §3, 02 §6), and core code
never names a vendor field or type (``test_core_has_no_servicenow_types``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from supportagent.domain.connector import ConnectorManifest, TicketSemantics
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord


class RecordType(StrEnum):
    """The kinds of record a connector polls and writes (01 §7.5, WP-M07).

    The values are the stored keys (cursor rows, ``cases.external_table``,
    action scopes) and stay as they were in v0.0, so upgrading changes no
    data; each connector maps them to its own record names.
    """

    INCIDENT = "incident"
    ALERT = "em_alert"


class NoteVisibility(StrEnum):
    """Who can read an appended note (01 §7.5, WP-M07).

    ``internal``: the support team only (ServiceNow ``work_notes``).
    ``customer``: the requester too (ServiceNow ``comments``). The connector
    maps each to its own note field.
    """

    INTERNAL = "internal"
    CUSTOMER = "customer"


@dataclass(frozen=True)
class PollBatch:
    """One page of ``TicketSource.poll`` (02 §6).

    ``records`` are the current state of each changed record in the window,
    ordered by ``(updated_at, external_id)``. ``next_cursor`` is the maximum
    ``updated_at`` seen in the batch; the caller advances its per-type cursor
    to it (ARCH §8.1). A full page means more may follow: the caller fetches
    the next page with ``offset += limit``.
    """

    records: Sequence[TicketRecord]
    next_cursor: datetime


class TicketSource(Protocol):
    """One incident system's read surface (02 §6)."""

    def poll(
        self, record_type: RecordType, since: datetime, limit: int, offset: int = 0
    ) -> PollBatch:
        """Records of ``record_type`` updated at or after ``since`` (one page).

        ``limit`` is the page size and ``offset`` the number of matching
        records to skip. The result is ordered by ``(updated_at,
        external_id)``, so paging by offset from a fixed ``since`` reaches
        every record even when more than ``limit`` share one second.
        """
        ...

    def record(self, record_type: RecordType, external_id: str) -> TicketRecord | None:
        """The record's current fields, or ``None`` when it doesn't exist (ADR-0010)."""
        ...

    def history(
        self,
        record_type: RecordType,
        external_id: str,
        after_mod_count: int,
        since: datetime | None = None,
    ) -> Sequence[ChangeRecord]:
        """Field and note changes for ``external_id`` (02 §6 ``history``).

        Field changes newer than revision ``after_mod_count`` plus the notes
        created at or after ``since`` (all of them when ``None``), ordered by
        ``ts``. Only called when the manifest declares ``history``.
        """
        ...


class TicketSink(Protocol):
    """One incident system's write surface (01 §7.5). All writes idempotent."""

    def append_journal(
        self,
        record_type: RecordType,
        external_id: str,
        visibility: NoteVisibility,
        text: str,
    ) -> ExternalRef:
        """Append ``text`` as the newest ``visibility`` note on ``external_id``.

        ``text`` already carries the ``[sa:<key>]`` marker as its last line
        (the handler appends it); the sink stores it verbatim.
        """
        ...

    def update_fields(
        self,
        record_type: RecordType,
        external_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        """Set ``fields`` on ``external_id`` (read-compare first: a no-op if equal)."""
        ...

    def find_marker(
        self,
        record_type: RecordType,
        external_id: str,
        marker: str,
    ) -> ExternalRef | None:
        """Reconciliation read (INV-04): the note carrying ``marker``, if any.

        Returns the ``ExternalRef`` of the note that already carries the
        marker (so ``mark_done`` can record it), or ``None`` if the action was
        never applied externally.
        """
        ...


class Connector(TicketSource, TicketSink, Protocol):
    """One incident system: read, write, what it can do, and how it maps (WP-M07)."""

    @property
    def manifest(self) -> ConnectorManifest: ...

    @property
    def semantics(self) -> TicketSemantics: ...


__all__ = [
    "Connector",
    "NoteVisibility",
    "PollBatch",
    "RecordType",
    "TicketSink",
    "TicketSource",
]
