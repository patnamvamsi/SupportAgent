"""WP-012: ``supportagent grants request --from config`` — wiring and exit codes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.cli import grants as grants_cli
from supportagent.entrypoints.cli.main import app


def test_grants_request_from_config_reports_count(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def load(path: Path) -> str:
        seen.append(path)
        return "cfg"

    monkeypatch.setattr(grants_cli, "load_config_default", load)
    monkeypatch.setattr(grants_cli, "request_grants_from_config", lambda cfg: ["g1", "g2"])
    result = CliRunner().invoke(
        app, ["grants", "request", "--from", "config", "--config", "x.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert "requested 2 grant(s)" in result.output
    assert seen == [Path("x.yaml")]


def test_grants_request_config_error_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: Path) -> Any:
        raise ConfigError("grants_request 'dag:x*' matches no catalog dags")  # noqa: TRY003

    monkeypatch.setattr(grants_cli, "load_config_default", boom)
    result = CliRunner().invoke(app, ["grants", "request", "--from", "config"])
    assert result.exit_code == 2
    assert "matches no catalog" in result.output


def test_grants_request_requires_from() -> None:
    result = CliRunner().invoke(app, ["grants", "request"])
    assert result.exit_code != 0
