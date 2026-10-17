"""Severity levels P1..P4 (01 §4, 01 §7.3: P1 > P2 > P3 > P4)."""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


_RANKS: dict[Severity, int] = {Severity.P1: 1, Severity.P2: 2, Severity.P3: 3, Severity.P4: 4}


def rank(severity: Severity) -> int:
    """Ordinal rank; smaller is more severe (P1 -> 1 ... P4 -> 4)."""
    return _RANKS[severity]
