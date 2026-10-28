"""Action handlers (WP-006, 01 §7.5): one executor step per ActionKind.

A ``Handler`` turns one outbox row into one external write through the
``TicketSink`` port. Journal kinds (``work_note``, ``customer_comment``) append
an entry whose **last line** is the idempotency marker ``[sa:<key>]``
(01 §7.5) — the reconciliation read (``find_marker``) keys on exactly that
line. Field-update kinds (``ticket_update``) carry no marker: their
idempotency comes from the sink's read-compare no-op (01 §7.5), so they skip
the reconciliation read.

Only the three executable v0.0 kinds are registered (10-wp WP-006);
``build_handlers(paging)`` adds ``enrich_page`` (11-wp WP-112). The other
``ActionKind`` members (``create_page`` ...) are never executable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.paging import PagingIntegration
from supportagent.ports.tickets import NoteVisibility, RecordType, TicketSink


class Handler(Protocol):
    """Executes one outbox row against one ticket (01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: RecordType,
        external_id: str,
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
        table: RecordType,
        external_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        return sink.append_journal(
            table, external_id, NoteVisibility.INTERNAL, _journal_text(payload, idempotency_key)
        )


class CustomerCommentHandler:
    """Append to ``comments`` (01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: RecordType,
        external_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        return sink.append_journal(
            table, external_id, NoteVisibility.CUSTOMER, _journal_text(payload, idempotency_key)
        )


class TicketUpdateHandler:
    """Set fields (idempotent read-compare; no marker, 01 §7.5)."""

    def execute(
        self,
        *,
        sink: TicketSink,
        table: RecordType,
        external_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,  # part of the Handler contract signature
    ) -> ExternalRef:
        return sink.update_fields(table, external_id, _fields(payload))


#: ``enrich_page`` found no open page: done, with nothing attached (ARCH §14).
NO_OPEN_PAGE = ExternalRef(system="paging", ref="no-open-page")


class EnrichPageHandler:
    """Attach a note to the *existing* page for the case (ARCH §14, T1).

    The payload names the catalog ``integration`` and the alert's dedup
    ``source_ref``. No open page is a no-op (``paging:no-open-page``), never a
    new page. The note's last line is the marker, and a note already carrying
    it is returned instead of a second one (INV-04 effectively-once).
    """

    def __init__(self, paging: PagingIntegration) -> None:
        self._paging = paging

    def execute(
        self,
        *,
        sink: TicketSink,  # part of the Handler contract; a page isn't a ticket
        table: RecordType,
        external_id: str,
        payload: Mapping[str, object],
        idempotency_key: str,
    ) -> ExternalRef:
        integration, source_ref = payload.get("integration"), payload.get("source_ref")
        if not isinstance(integration, str) or not isinstance(source_ref, str):
            raise DomainValidationError(  # noqa: TRY003
                "enrich_page payload must carry 'integration' and 'source_ref' strings"
            )
        text = _journal_text(payload, idempotency_key)
        page = self._paging.find_open_page(integration, source_ref)
        if page is None:
            return NO_OPEN_PAGE
        existing = self._paging.find_note(page, marker(idempotency_key))
        if existing is not None:
            return existing
        return self._paging.add_note(page, text)


#: The executable v0.0 actions (10-wp WP-006) and their handlers.
HANDLERS: Mapping[ActionKind, Handler] = {
    ActionKind.WORK_NOTE: WorkNoteHandler(),
    ActionKind.CUSTOMER_COMMENT: CustomerCommentHandler(),
    ActionKind.TICKET_UPDATE: TicketUpdateHandler(),
}


def build_handlers(paging: PagingIntegration | None = None) -> dict[ActionKind, Handler]:
    """The v0.0 handlers, plus ``enrich_page`` when a paging integration is wired."""
    handlers: dict[ActionKind, Handler] = dict(HANDLERS)
    if paging is not None:
        handlers[ActionKind.ENRICH_PAGE] = EnrichPageHandler(paging)
    return handlers


#: Kinds whose effect is a journal entry — reconciled via ``find_marker``
#: (INV-04). ``ticket_update`` reconciles via the sink's read-compare instead.
JOURNAL_KINDS: frozenset[ActionKind] = frozenset(
    {ActionKind.WORK_NOTE, ActionKind.CUSTOMER_COMMENT}
)


__all__ = [
    "HANDLERS",
    "JOURNAL_KINDS",
    "NO_OPEN_PAGE",
    "CustomerCommentHandler",
    "EnrichPageHandler",
    "Handler",
    "TicketUpdateHandler",
    "WorkNoteHandler",
    "build_handlers",
    "marker",
]
