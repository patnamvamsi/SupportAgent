"""``supportagent worker`` / ``run`` (02 §4): wiring, ``--once`` and the drain."""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.cli import worker as worker_cli
from supportagent.entrypoints.cli.main import app
from supportagent.entrypoints.worker.jobs import JobResult


class _Loop:
    def __init__(self) -> None:
        self.ran = False
        self.stopped = False

    def run_once(self) -> list[JobResult]:
        return [
            JobResult(name="sync.poll.incident", ran=True, rows=2),
            JobResult(name="sync.poll.em_alert", ran=False),
        ]

    def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True


def test_worker_once_prints_each_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_cli, "load_config_default", lambda path: f"cfg:{path}")
    monkeypatch.setattr(worker_cli, "build_worker", lambda cfg: _Loop())
    out = CliRunner().invoke(app, ["worker", "--once", "--config", "c.yaml"])
    assert out.exit_code == 0, out.output
    assert "sync.poll.incident: 2 row(s)" in out.output
    assert "sync.poll.em_alert: skipped (lease held elsewhere)" in out.output


def test_worker_runs_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop()
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(worker_cli, "load_config_default", lambda path: "cfg")
    monkeypatch.setattr(worker_cli, "build_worker", lambda cfg: loop)
    monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.update({sig: h}))
    out = CliRunner().invoke(app, ["worker"])
    assert out.exit_code == 0, out.output
    assert loop.ran
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert loop.stopped, "SIGTERM drains: the loop stops after the job in progress"


def test_worker_config_error_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: Path) -> Any:
        raise ConfigError("calibration missing")  # noqa: TRY003

    monkeypatch.setattr(worker_cli, "load_config_default", boom)
    out = CliRunner().invoke(app, ["worker", "--once"])
    assert out.exit_code == 2
    assert "calibration missing" in out.output


def test_run_serves_api_and_stops_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _Loop()
    served: dict[str, Any] = {}
    monkeypatch.setattr(worker_cli, "load_config_default", lambda path: "cfg")
    monkeypatch.setattr(worker_cli, "build_api_context", lambda cfg: "ctx")
    monkeypatch.setattr(worker_cli, "create_app", lambda ctx: f"app:{ctx}")
    monkeypatch.setattr(worker_cli, "build_worker", lambda cfg: loop)
    monkeypatch.setattr("uvicorn.run", lambda a, **kw: served.update(app=a, **kw))
    out = CliRunner().invoke(app, ["run", "--port", "9002"])
    assert out.exit_code == 0, out.output
    assert (served["app"], served["port"]) == ("app:ctx", 9002)
    assert loop.ran and loop.stopped, "the worker thread ran and was stopped on shutdown"
