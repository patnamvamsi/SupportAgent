"""Triage result and decision (01 §4)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity


class TriageResult(DomainModel):
    """The classifier's output (01 §4)."""

    classification: str = Field(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    severity: Severity
    summary: str = Field(max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class TriageDecision(DomainModel):
    """Why the case routed (or safe-defaulted) — one of the 7 reasons (01 §4)."""

    outcome: Literal["routed", "safe_default"]
    reason: Literal[
        "llm-failure",
        "classifier-conflict",
        "low-calibrated-score",
        "unconfirmed-high-severity",
        "unresolved-service",
        "spend-breaker",
        "routed",
    ]
    classification: str | None
    severity: Severity | None
    preclass_label: str | None
    calibrated_score: float | None
    route_id: str | None
    llm_suggestion: TriageResult | None
