"""A ticketless case's outcome, as a person recorded it (ADR-0024).

An alerts-only system has no ticket to resolve a case. A person resolves it on the
case page and records what it really was. Each recorded outcome is a calibration
observation of the label triage gave, and, with no incident history, a member of
its label's readiness class.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    """ADR-0024: one ticketless case, resolved by a person who recorded its outcome."""

    case_id: str
    #: What the person said it was, and how severe.
    label: str
    severity: str | None
    #: Their verdict on the RCA's supported cause (``None``: it supported none).
    cause: str | None
    drill: bool
    #: What triage said: its label (or suggestion), the pre-classifier's, and the decision.
    predicted: str | None
    preclass: str | None
    outcome: str
    reason: str
    triage_severity: str | None
    #: The RCA's verdict and the hypothesis it supported, if it ran.
    verdict: str | None
    supported: str | None


__all__ = ["AlertOutcome"]
