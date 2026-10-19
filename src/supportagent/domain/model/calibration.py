"""Calibration table produced by an eval run (ADR-0002, 01 §7.7)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.model.base import DomainModel


class CalibrationCountRow(DomainModel):
    """``n`` trials, ``correct`` of them matching the reference (01 §7.7)."""

    n: int = Field(ge=0)
    correct: int = Field(ge=0)

    def _assert_domain_rules(self) -> None:
        if self.correct > self.n:
            # 01 §7.1: the message is the pinned validation API surface.
            raise DomainValidationError(  # noqa: TRY003
                f"correct ({self.correct}) cannot exceed n ({self.n})"
            )


class CalibrationLabelRow(DomainModel):
    """Per-label counts, split by whether the pre-classifier agreed (01 §7.7)."""

    agree: CalibrationCountRow | None = None
    abstain: CalibrationCountRow | None = None


class CalibrationTable(DomainModel):
    """The calibration artifact for one model + suite grade (ADR-0002).

    Produced only by eval runs: the smoke suite (WP-016, ``grade: smoke``) or
    the quality suite (WP-105, ``grade: holdout``). ``grade``/``snapshot_id``/
    ``model_id`` are recorded so a stale or mismatched artifact is rejected at
    load (see ``adapters.files.calibration_loader``).
    """

    #: Artifact format version written by the producer (01 §7.7 example).
    version: int | None = Field(default=None, ge=1)
    grade: Literal["smoke", "holdout"]
    snapshot_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    labels: dict[str, CalibrationLabelRow]
