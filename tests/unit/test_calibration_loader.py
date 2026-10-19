"""WP-010: the calibration loader rejects grade/model mismatch (ADR-0002).

Canonical test IDs from 10-wp §WP-010:
``test_calibration_grade_below_min_rejected``, ``test_calibration_model_mismatch_rejected``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.domain.errors import ConfigError

_MODEL = "claude-sonnet-4-5-20250929"


def _write(path: Path, *, grade: str, model_id: str) -> Path:
    data = {
        "grade": grade,
        "snapshot_id": "snap-1",
        "model_id": model_id,
        "labels": {"network/dns": {"agree": {"n": 35, "correct": 35}}},
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_calibration_grade_below_min_rejected(tmp_path: Path) -> None:
    # ADR-0002: an artifact whose grade is below calibration.min_grade is
    # rejected at load (v0.0 floor is ``smoke``; v0.1 raises it to ``holdout``).
    path = _write(tmp_path / "calibration.json", grade="smoke", model_id=_MODEL)
    with pytest.raises(ConfigError, match="grade"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="holdout")


def test_calibration_model_mismatch_rejected(tmp_path: Path) -> None:
    # ADR-0002: an artifact whose model_id differs from the configured classify
    # model is rejected at load (a model change invalidates the calibration).
    path = _write(tmp_path / "calibration.json", grade="holdout", model_id="claude-old-model")
    with pytest.raises(ConfigError, match="model"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")
