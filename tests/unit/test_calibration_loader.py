"""WP-010: the calibration loader rejects grade/model mismatch (ADR-0002).

Canonical test IDs from 10-wp §WP-010:
``test_calibration_grade_below_min_rejected``, ``test_calibration_model_mismatch_rejected``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.domain.errors import CalibrationMissing, ConfigError
from supportagent.domain.model.calibration import CalibrationTable

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


def test_calibration_loads_spec_example_with_version(tmp_path: Path) -> None:
    # 01 §7.7: the artifact the quality suite writes carries a ``version``.
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "grade": "holdout",
                "snapshot_id": "snap-2026-11-02",
                "model_id": _MODEL,
                "labels": {
                    "database/saturation": {
                        "agree": {"n": 41, "correct": 39},
                        "abstain": {"n": 18, "correct": 15},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    # WP-105: a holdout table must be signed; the spec example is, once its run signs it.
    with pytest.raises(ConfigError, match="must be signed"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")
    signed = CalibrationTable.model_validate_json(path.read_text()).signed("run-2026-11-02")
    path.write_text(signed.model_dump_json(), encoding="utf-8")
    table = load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")
    assert table.version == 3


@pytest.mark.parametrize("content", [None, "{not json", "[]"], ids=["missing", "garbage", "list"])
def test_calibration_unreadable_is_config_error(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "calibration.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="calibration"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")


def test_calibration_missing_file_is_distinguished(tmp_path: Path) -> None:
    # Not written yet is the expected pre-calibration state (doctor warns);
    # it is still a ConfigError, so strict callers keep refusing it.
    with pytest.raises(CalibrationMissing, match="not written yet") as info:
        load_calibration(tmp_path / "absent.json", configured_model_id=_MODEL, min_grade="smoke")
    assert isinstance(info.value, ConfigError)
