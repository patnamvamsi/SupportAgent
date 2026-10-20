"""WP-105: the holdout-grade calibration table (ADR-0002, 01 §7.7).

Canonical test IDs from 11-wp §WP-105: ``test_calibration_holdout_grade_written``,
``test_calibration_signature_verified``, ``test_calibration_hand_edit_rejected``,
``test_v01_rejects_smoke_grade``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.domain.classification import calibrated_score
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.quality import CaseOutcome
from supportagent.entrypoints.cli.main import app
from supportagent.evals.calibration import build_holdout_calibration
from tests.unit.test_triage_service import _shadow_config

_MODEL = "claude-sonnet-4-5-20250929"


def _holdout(n: int = 12) -> CalibrationTable:
    outcomes = [
        CaseOutcome(
            f"c{i}",
            "database/saturation",
            "P2",
            [("database/saturation", "P2")] * 3 if i < n - 1 else [("network/x", "P2")],
        )
        for i in range(n)
    ]
    preclass = {f"c{i}": "database/saturation" if i % 2 else None for i in range(n)}
    return build_holdout_calibration(
        outcomes,
        preclass,
        model_id=_MODEL,
        snapshot_id="snap-h1",
        report_id="20261102T0900Z-quality-live",
    )


def _write(tmp_path: Path, table: CalibrationTable) -> Path:
    path = tmp_path / "calibration.json"
    path.write_text(table.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_calibration_holdout_grade_written(tmp_path: Path) -> None:
    table = _holdout()
    assert (table.grade, table.snapshot_id, table.report_id) == (
        "holdout",
        "snap-h1",
        "20261102T0900Z-quality-live",
    )
    row = table.labels["database/saturation"]
    assert row.agree is not None and row.abstain is not None
    assert (row.agree.n, row.abstain.n) == (5, 6), "one observation per case, by majority"
    assert "network/x" not in table.labels, "a pre-classifier conflict is not evidence"
    loaded = load_calibration(
        _write(tmp_path, table), configured_model_id=_MODEL, min_grade="holdout"
    )
    assert calibrated_score(loaded, "database/saturation", preclass_agrees=False) == 0.0, (
        "n < 10 still scores 0 (ADR-0002)"
    )


def test_calibration_signature_verified(tmp_path: Path) -> None:
    table = _holdout()
    assert table.signature == table.computed_signature()
    load_calibration(_write(tmp_path, table), configured_model_id=_MODEL, min_grade="holdout")
    unsigned = table.model_copy(update={"signature": None})
    with pytest.raises(ConfigError, match="must be signed"):
        load_calibration(_write(tmp_path, unsigned), configured_model_id=_MODEL, min_grade="smoke")


@pytest.mark.parametrize("edit", ["count", "grade_field", "report"])
def test_calibration_hand_edit_rejected(tmp_path: Path, edit: str) -> None:
    path = _write(tmp_path, _holdout())
    data = json.loads(path.read_text())
    if edit == "count":
        data["labels"]["database/saturation"]["abstain"]["correct"] = 5
    elif edit == "grade_field":
        data["snapshot_id"] = "snap-other"
    else:
        data["report_id"] = "a-better-run"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="signature mismatch"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")


def test_v01_rejects_smoke_grade(tmp_path: Path) -> None:
    smoke = CalibrationTable(grade="smoke", snapshot_id="s", model_id=_MODEL, labels={})
    path = _write(tmp_path, smoke)
    with pytest.raises(ConfigError, match="below the required 'holdout'"):
        load_calibration(path, configured_model_id=_MODEL, min_grade="holdout")
    load_calibration(path, configured_model_id=_MODEL, min_grade="smoke")  # v0.0 still loads
    data = _shadow_config().model_dump(mode="json")
    data["calibration"]["min_grade"] = "holdout"
    assert (
        type(_shadow_config())
        .model_validate(
            {
                **data,
                "identity": _shadow_config().identity,
                "servicenow": _shadow_config().servicenow,
                "database": _shadow_config().database,
            }
        )
        .calibration.min_grade
        == "holdout"
    )


def test_quality_calibration_needs_live() -> None:
    out = CliRunner().invoke(app, ["eval", "run", "--suite", "quality", "--write-calibration"])
    assert out.exit_code == 2 and "--live" in out.output
