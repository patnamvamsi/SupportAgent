"""Action handlers (WP-006, 01 §7.5): one executor step per ActionKind.

A ``Handler`` turns one outbox row into one external write through the
``TicketSink`` port. Journal kinds (``work_note``, ``customer_comment``) append
an entry whose **last line** is the idempotency marker ``[sa:<key>]``
(01 §7.5) — the reconciliation read (``find_marker``) keys on exactly that
line. Field-update kinds (``ticket_update``) carry no marker: their
idempotency comes from the sink's read-compare no-op (01 §7.5), so they skip
the reconciliation read.

Only the three executable v0.0 kinds are registered (10-wp WP-006); the
remaining ``ActionKind`` members (``create_page`` ...) are never executable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import JournalField, SnTable, TicketSink


class Handler(Protocol):
    """Executes one outbox row against one ticket (01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: SnTable,
        sys_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        """Apply the action; return the external reference of the write."""
        ...


def marker(idempotency_key: str) -> str:
    """The journal marker line (01 §7.5): ``[sa:<key>]``."""
    return f"[sa:{idempotency_key}]"


def _journal_text(payload: Mapping[str, object], idempotency_key: str) -> str:
    """The journal entry: the payload text plus the marker as last line."""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise DomainValidationError(  # noqa: TRY003
            "journal payload must carry a non-empty 'text' string (01 §7.5)"
        )
    return f"{text}\n{marker(idempotency_key)}"


def _fields(payload: Mapping[str, object]) -> Mapping[str, str]:
    fields = payload.get("fields")
    if (
        not isinstance(fields, Mapping)
        or not fields
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())
    ):
        raise DomainValidationError(  # noqa: TRY003
            "ticket_update payload must carry a non-empty 'fields' mapping of str to str"
        )
    return fields


class WorkNoteHandler:
    """Append to ``work_notes`` (01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: SnTable,
        sys_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        return sink.append_journal(
            table, sys_id, JournalField.WORK_NOTES, _journal_text(payload, idempotency_key)
        )


class CustomerCommentHandler:
    """Append to ``comments`` (01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: SnTable,
        sys_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        return sink.append_journal(
            table, sys_id, JournalField.COMMENTS, _journal_text(payload, idempotency_key)
        )


class TicketUpdateHandler:
    """Set fields (idempotent read-compare; no marker, 01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: SnTable,
        sys_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,  # part of the Handler contract signature
    ) -> ExternalRef:
        return sink.update_fields(table, sys_id, _fields(payload))


#: The executable v0.0 actions (10-wp WP-006) and their handlers.
HANDLERS: Mapping[ActionKind, Handler] = {
    ActionKind.WORK_NOTE: WorkNoteHandler(),
    ActionKind.CUSTOMER_COMMENT: CustomerCommentHandler(),
    ActionKind.TICKET_UPDATE: TicketUpdateHandler(),
}

#: Kinds whose effect is a journal entry — reconciled via ``find_marker``
#: (INV-04). ``ticket_update`` reconciles via the sink's read-compare instead.
JOURNAL_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.WORK_NOTE, ActionKind.CUSTOMER_COMMENT}
)


__all__ = [
    "HANDLERS",
    "JOURNAL_KINDS",
    "CustomerCommentHandler",
    "Handler",
    "TicketUpdateHandler",
    "WorkNoteHandler",
    "marker",
]
