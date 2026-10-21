"""Severity levels P1..P4 (01 §4, 01 §7.3: P1 > P2 > P3 > P4)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supportagent.domain.model.alert import AlertEvent


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


_RANKS: dict[Severity, int] = {Severity.P1: 1, Severity.P2: 2, Severity.P3: 3, Severity.P4: 4}


def rank(severity: Severity) -> int:
    """Ordinal rank; smaller is more severe (P1 -> 1 ... P4 -> 4)."""
    return _RANKS[severity]


def severity_escalated(evt: AlertEvent, case: Mapping[str, object]) -> bool:
    """True iff ``evt.severity_hint`` ranks above the case's current severity.

    Escalation is the only material change that re-triages an existing case
    in place (01 §7.3, INV-02): strict ``rank(hint) < rank(current)`` with
    P1 > P2 > P3 > P4. A hint against a case without a severity yet is an
    escalation (the case acquires its first ranked severity); no hint, or a
    hint at/below the current severity, is not. ``case`` is a row mapping
    (the ``cases`` row, 03 §2.2). The severity-less case is intended (BL-018):
    a case first seen without a hint gets triaged with its first real severity.
    """
    hint = evt.severity_hint
    if hint is None:
        return False
    current_raw = case.get("severity")
    current = Severity(str(current_raw)) if current_raw is not None else None
    if current is None:
        return True
    return rank(hint) < rank(current)
