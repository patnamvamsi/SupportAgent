"""BL-018 / 01 §7.7: a rejected calibration table means every label scores 0, not a dead worker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.bootstrap import (
    REJECTED_CALIBRATION,
    _build_calibration,
    _classifier_calibration,
    build_worker,
)
from supportagent.config.schema import AppConfig
from supportagent.domain.classification import calibrated_score
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.calibration import CalibrationTable
from tests.integration.test_shadow_worker import _config


def _mismatched(tmp_path: Path) -> AppConfig:
    config = _config(tmp_path)
    Path(config.calibration.path).write_text(
        CalibrationTable(
            grade="smoke", snapshot_id="s", model_id="some-other-model-20250101", labels={}
        ).model_dump_json(),
        encoding="utf-8",
    )
    return config


def test_rejected_calibration_scores_every_label_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _mismatched(tmp_path)
    with pytest.raises(ConfigError, match="does not match"):
        _build_calibration(config)  # doctor's strict path still fails, with the reason
    with caplog.at_level(logging.ERROR, logger="supportagent.bootstrap"):
        table = _classifier_calibration(config)
    assert table.snapshot_id == REJECTED_CALIBRATION
    assert table.labels == {}
    assert calibrated_score(table, "database/saturation", preclass_agrees=True) == 0.0
    assert "calibration rejected" in caplog.text


def test_worker_starts_with_a_rejected_calibration(
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes
) -> None:
    worker = build_worker(
        _mismatched(tmp_path),
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(FakeServiceNowHTTP().app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )
    assert worker.jobs
