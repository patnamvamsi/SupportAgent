"""File adapter for the calibration artifact (ADR-0002, 01 §7.7)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from supportagent.domain.errors import ConfigError
from supportagent.domain.model import CalibrationTable

_GRADE_RANK = {"smoke": 0, "holdout": 1}


def load_calibration(
    path: Path,
    *,
    configured_model_id: str,
    min_grade: str,
) -> CalibrationTable:
    """Load a calibration artifact, rejecting a mismatched model or grade.

    ADR-0002: the artifact is produced only by eval runs and records the model
    and suite grade it was measured under. It is rejected at load when its
    ``model_id`` differs from the configured classify model, or its grade is
    below ``calibration.min_grade`` (ranks: ``smoke`` < ``holdout``).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # unreadable, or not JSON (JSONDecodeError)
        raise ConfigError(f"calibration {path}: {exc}") from exc  # noqa: TRY003
    try:
        table = CalibrationTable.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"calibration {path}: {exc}") from exc  # noqa: TRY003

    if table.model_id != configured_model_id:
        raise ConfigError(  # noqa: TRY003 — message is the pinned load API (ADR-0002)
            f"calibration {path}: model_id {table.model_id!r} does not match the "
            f"configured classify model {configured_model_id!r}"
        )
    if min_grade not in _GRADE_RANK:
        raise ConfigError(f"calibration {path}: unknown min_grade {min_grade!r}")  # noqa: TRY003
    if _GRADE_RANK[table.grade] < _GRADE_RANK[min_grade]:
        raise ConfigError(  # noqa: TRY003 — message is the pinned load API (ADR-0002)
            f"calibration {path}: grade {table.grade!r} is below the required {min_grade!r}"
        )
    # WP-105: a holdout table must carry its signature; any signed table is verified.
    if table.grade == "holdout" and (table.signature is None or table.report_id is None):
        raise ConfigError(  # noqa: TRY003
            f"calibration {path}: a holdout table must be signed (report_id + signature); "
            "write it with `eval run --suite quality --live --write-calibration`"
        )
    if table.signature is not None and table.signature != table.computed_signature():
        raise ConfigError(  # noqa: TRY003
            f"calibration {path}: signature mismatch; the counts were edited after the eval "
            "run that produced them. Re-run the suite instead."
        )
    return table
