"""``severity_escalated`` (01 §7.3, INV-02) — the only material change that
re-triages an existing case in place.

Escalation is strict: ``evt.severity_hint`` ranks above the case's current
severity (P1 > P2 > P3 > P4). Same or lower is not an escalation; a missing
hint is never an escalation.
"""

from __future__ import annotations

from typing import Any

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.severity import Severity, severity_escalated
from tests.builders import an_alert


def _event(hint: Severity | None) -> AlertEvent:
    return AlertEvent(**an_alert(severity_hint=hint))


def _case(current: str | None) -> dict[str, Any]:
    return {"severity": current}


def test_severity_escalation_detected() -> None:
    assert severity_escalated(_event(Severity.P1), _case("P3")) is True
    assert severity_escalated(_event(Severity.P2), _case("P4")) is True
    assert severity_escalated(_event(Severity.P1), _case("P2")) is True
    # A hint ranks above a case that has no severity yet.
    assert severity_escalated(_event(Severity.P3), _case(None)) is True


def test_severity_same_or_lower_not_escalation() -> None:
    assert severity_escalated(_event(Severity.P3), _case("P3")) is False
    assert severity_escalated(_event(Severity.P4), _case("P3")) is False
    assert severity_escalated(_event(Severity.P2), _case("P1")) is False
    assert severity_escalated(_event(None), _case("P3")) is False
