"""Proposal state machine (01 §7.10)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from supportagent.domain.errors import DomainValidationError


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    SUPERSEDED = "superseded"


class ProposalEvent(StrEnum):
    SUBMITTED = "submitted"
    GRANT_MATCHED = "grant_matched"
    APPROVED = "approved"
    REJECTED = "rejected"
    TTL_ELAPSED = "ttl_elapsed"
    CASE_RETRIAGED = "case_retriaged"
    OUTBOX_INSERTED = "outbox_inserted"


# The full legal transition set (01 §7.10); anything else is illegal.
TRANSITIONS: Mapping[tuple[ProposalStatus, ProposalEvent], ProposalStatus] = {
    (ProposalStatus.PROPOSED, ProposalEvent.SUBMITTED): ProposalStatus.PENDING_APPROVAL,
    (ProposalStatus.PROPOSED, ProposalEvent.GRANT_MATCHED): ProposalStatus.APPROVED,
    (ProposalStatus.PENDING_APPROVAL, ProposalEvent.APPROVED): ProposalStatus.APPROVED,
    (ProposalStatus.PENDING_APPROVAL, ProposalEvent.REJECTED): ProposalStatus.REJECTED,
    (ProposalStatus.PENDING_APPROVAL, ProposalEvent.TTL_ELAPSED): ProposalStatus.EXPIRED,
    (
        ProposalStatus.PENDING_APPROVAL,
        ProposalEvent.CASE_RETRIAGED,
    ): ProposalStatus.SUPERSEDED,
    (
        ProposalStatus.APPROVED,
        ProposalEvent.CASE_RETRIAGED,
    ): ProposalStatus.SUPERSEDED,
    (ProposalStatus.APPROVED, ProposalEvent.OUTBOX_INSERTED): ProposalStatus.EXECUTED,
}


def next_status(current: ProposalStatus, event: ProposalEvent) -> ProposalStatus:
    """The status after a legal event; a DomainValidationError otherwise."""
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise DomainValidationError(  # noqa: TRY003 — the message names the rejected transition
            f"illegal proposal transition: {current.value} + {event.value}"
        ) from None
