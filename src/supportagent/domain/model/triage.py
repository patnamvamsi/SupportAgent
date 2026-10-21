"""Triage result and decision (01 §4)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.severity import Severity


class TriageResult(DomainModel):
    """The classifier's output (01 §4)."""

    classification: str = Field(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    severity: Severity
    summary: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class TriageDecision(DomainModel):
    """Why the case routed (or safe-defaulted) — one of the 8 reasons (01 §4, ADR-0006)."""

    outcome: Literal["routed", "safe_default"]
    reason: Literal[
        "llm-failure",
        "classifier-conflict",
        "low-calibrated-score",
        "unconfirmed-high-severity",
        "unresolved-service",
        "no-paging-integration",
        "storm-member",
        "spend-breaker",
        "routed",
    ]
    classification: str | None
    severity: Severity | None
    preclass_label: str | None
    calibrated_score: float | None
    route_id: str | None
    llm_suggestion: TriageResult | None


class Linked(DomainModel):
    """A duplicate alert linked to an existing case (no re-decision, 10-wp WP-011)."""

    case_id: str


class Retriaged(DomainModel):
    """An existing case re-decided in place (severity escalation, ARCH §4.1).

    A routed re-decision is routed again: the case's pending proposals are
    superseded and ``proposals`` holds the new ones (BL-019). A human-owned
    case keeps its proposals and gets none.
    """

    case_id: str
    decision: TriageDecision
    proposals: list[Proposal] = Field(default_factory=list)
    superseded: list[str] = Field(default_factory=list)


class Triaged(DomainModel):
    """A new case routed, with its proposals submitted through the gate."""

    case_id: str
    decision: TriageDecision
    proposals: list[Proposal]


class SafeDefaulted(DomainModel):
    """A new case that hit a safe default; the case, decision and events are written,
    but nothing is proposed (ARCH §4)."""

    case_id: str
    decision: TriageDecision


TriageOutcome = Linked | Retriaged | Triaged | SafeDefaulted
