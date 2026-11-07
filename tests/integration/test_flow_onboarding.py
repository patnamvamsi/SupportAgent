"""ADR-0021 §2-§3 on the database: candidate flows, drafts, and correlation checks.

``flow verify`` runs each stage's lookup against objects a person says
completed the flow; a stage that finds none of them is a correlation gap.
``onboard flows`` on a reference workspace whose tickets are all about
infrastructure proposes nothing, and says so.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine
from typer.testing import CliRunner

from supportagent.bootstrap import (
    ONBOARDING_DIR,
    _UnrecordedProvider,
    load_flow_file,
    onboard_flows,
    verify_flow,
)
from supportagent.domain.errors import ConfigError
from tests.integration.test_trace_db import _setup
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.integration


def test_verify_finds_a_correlation_gap(tmp_path: Path, db: Engine, master_key: bytes) -> None:
    config = _setup(tmp_path)
    flow = load_flow_file(Path(config.tracing.flows_dir) / "trade-settlement.yaml")
    # Settlement's log never carries the id: every completed trade misses there.
    report = verify_flow(config, flow, ["T-1234", "T-9999"], engine=db, master_key=master_key)
    by_stage = {r.stage: r for r in report}
    assert (by_stage["submit"].found, by_stage["submit"].gap) == (2, False)
    assert by_stage["settle"].gap and by_stage["settle"].missing == 2


def test_verify_checks_drafts_and_sample_ids(tmp_path: Path, db: Engine, master_key: bytes) -> None:
    config = _setup(tmp_path)
    path = Path(config.tracing.flows_dir) / "trade-settlement.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["reviewed"] = False
    path.write_text(yaml.safe_dump(raw))
    flow = load_flow_file(path)
    report = verify_flow(config, flow, ["T-1234"], engine=db, master_key=master_key)
    assert report[0].found == 1  # a draft is verified, never traced
    with pytest.raises(ConfigError, match="doesn't accept"):
        verify_flow(config, flow, ["nope"], engine=db, master_key=master_key)
    with pytest.raises(ConfigError, match="at least one sample"):
        verify_flow(config, flow, [], engine=db, master_key=master_key)


def test_onboard_flows_on_an_infrastructure_history(tmp_path: Path) -> None:
    workspace = tmp_path / "system-c"
    shutil.copytree(ONBOARDING_DIR / "system-c", workspace)
    recordings = ONBOARDING_DIR / "recordings"
    if recordings.is_dir():
        shutil.copytree(recordings, workspace / "recordings", dirs_exist_ok=True)
    found, written = onboard_flows(_shadow_config(), workspace, provider=_UnrecordedProvider())
    assert found == [] and written == []
    assert yaml.safe_load((workspace / "flows.proposed" / "candidates.yaml").read_text()) == {
        "candidates": []
    }


def test_flow_verify_cli_exit_codes(tmp_path: Path) -> None:
    from supportagent.entrypoints.cli.main import app  # noqa: PLC0415

    res = CliRunner().invoke(
        app, ["flow", "verify", str(tmp_path / "missing.yaml"), "--sample", "x"]
    )
    assert res.exit_code == 2
