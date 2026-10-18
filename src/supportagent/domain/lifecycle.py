"""Case lifecycle state machine (01 §7.11)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from supportagent.domain.errors import DomainValidationError


class CaseStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    HUMAN_OWNED = "human_owned"
    RESOLVED = "resolved"
    REOPENED = "reopened"


class CaseEvent(StrEnum):
    TRIAGED = "triaged"
    SAFE_DEFAULTED = "safe_defaulted"
    WORK_STARTED = "work_started"
    HUMAN_TAKEOVER = "human_takeover"
    HAND_BACK = "hand_back"
    RESOLVED = "resolved"
    REOPENED = "reopened"


# The full legal transition set (01 §7.11); anything else is illegal.
TRANSITIONS: Mapping[tuple[CaseStatus, CaseEvent], CaseStatus] = {
    (CaseStatus.OPEN, CaseEvent.TRIAGED): CaseStatus.TRIAGED,
    (CaseStatus.OPEN, CaseEvent.SAFE_DEFAULTED): CaseStatus.TRIAGED,
    (CaseStatus.TRIAGED, CaseEvent.WORK_STARTED): CaseStatus.IN_PROGRESS,
    (CaseStatus.OPEN, CaseEvent.HUMAN_TAKEOVER): CaseStatus.HUMAN_OWNED,
    (CaseStatus.TRIAGED, CaseEvent.HUMAN_TAKEOVER): CaseStatus.HUMAN_OWNED,
    (CaseStatus.IN_PROGRESS, CaseEvent.HUMAN_TAKEOVER): CaseStatus.HUMAN_OWNED,
    (CaseStatus.REOPENED, CaseEvent.HUMAN_TAKEOVER): CaseStatus.HUMAN_OWNED,
    (CaseStatus.HUMAN_OWNED, CaseEvent.HAND_BACK): CaseStatus.IN_PROGRESS,
    (CaseStatus.OPEN, CaseEvent.RESOLVED): CaseStatus.RESOLVED,
    (CaseStatus.TRIAGED, CaseEvent.RESOLVED): CaseStatus.RESOLVED,
    (CaseStatus.IN_PROGRESS, CaseEvent.RESOLVED): CaseStatus.RESOLVED,
    (CaseStatus.HUMAN_OWNED, CaseEvent.RESOLVED): CaseStatus.RESOLVED,
    (CaseStatus.REOPENED, CaseEvent.RESOLVED): CaseStatus.RESOLVED,
    (CaseStatus.RESOLVED, CaseEvent.REOPENED): CaseStatus.REOPENED,
    (CaseStatus.REOPENED, CaseEvent.TRIAGED): CaseStatus.TRIAGED,
}


def next_status(current: CaseStatus, event: CaseEvent) -> CaseStatus:
    """The status after a legal event; a DomainValidationError otherwise."""
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise DomainValidationError(  # noqa: TRY003 — the message names the rejected transition
            f"illegal case transition: {current.value} + {event.value}"
        ) from None
