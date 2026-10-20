"""WP-013: ``supportagent serve`` wires the app from config and runs uvicorn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.cli import serve as serve_cli
from supportagent.entrypoints.cli.main import app


def test_serve_runs_uvicorn_with_the_built_app(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: dict[str, Any] = {}
    monkeypatch.setattr(serve_cli, "load_config_default", lambda path: f"cfg:{path}")
    monkeypatch.setattr(serve_cli, "build_api_context", lambda cfg: f"ctx:{cfg}")
    monkeypatch.setattr(serve_cli, "create_app", lambda ctx: f"app:{ctx}")
    monkeypatch.setattr("uvicorn.run", lambda a, **kw: ran.update(app=a, **kw))
    result = CliRunner().invoke(app, ["serve", "--config", "c.yaml", "--port", "9001"])
    assert result.exit_code == 0, result.output
    assert ran["app"] == "app:ctx:cfg:c.yaml"
    assert (ran["host"], ran["port"]) == ("127.0.0.1", 9001)


def test_serve_config_error_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: Path) -> Any:
        raise ConfigError("missing master key")  # noqa: TRY003

    monkeypatch.setattr(serve_cli, "load_config_default", boom)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 2
    assert "missing master key" in result.output
