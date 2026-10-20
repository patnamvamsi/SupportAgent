"""``supportagent doctor`` (02 §4): output, ``--json``, exit codes and ``--setup``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.app.doctor import CheckResult, DoctorReport
from supportagent.domain.errors import ConfigError
from supportagent.entrypoints.cli import doctor as doctor_cli
from supportagent.entrypoints.cli.main import app


def _report(*statuses: tuple[str, str]) -> DoctorReport:
    return DoctorReport(results=[CheckResult(n, s, f"{n} detail") for n, s in statuses])  # type: ignore[arg-type]


def _wire(monkeypatch: pytest.MonkeyPatch, report: DoctorReport) -> list[bool]:
    discovered: list[bool] = []
    monkeypatch.setattr(doctor_cli, "load_config_default", lambda path: "cfg")

    def fake_run(cfg: Any, *, discover_automation: bool) -> DoctorReport:
        discovered.append(discover_automation)
        return report

    monkeypatch.setattr(doctor_cli, "run_doctor", fake_run)
    return discovered


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        ((("config", "pass"), ("db", "pass")), 0),
        ((("config", "pass"), ("db", "warn")), 3),
        ((("config", "warn"), ("db", "fail")), 1),
    ],
)
def test_doctor_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, statuses: tuple[tuple[str, str], ...], code: int
) -> None:
    _wire(monkeypatch, _report(*statuses))
    out = CliRunner().invoke(app, ["doctor"])
    assert out.exit_code == code, out.output
    assert "config" in out.output and "db detail" in out.output


def test_doctor_cli_json_and_discover_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    discovered = _wire(monkeypatch, _report(("config", "pass")))
    out = CliRunner().invoke(app, ["doctor", "--json", "--discover-automation"])
    assert out.exit_code == 0
    body = json.loads(out.output)
    assert body["exit_code"] == 0
    assert body["checks"] == [{"name": "config", "status": "pass", "detail": "config detail"}]
    assert discovered == [True]


def test_doctor_cli_unloadable_config_is_the_config_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_: Path) -> Any:
        raise ConfigError("unknown key 'tenants'")  # noqa: TRY003

    monkeypatch.setattr(doctor_cli, "load_config_default", boom)
    out = CliRunner().invoke(app, ["doctor"])
    assert out.exit_code == 1
    assert "FAIL  config" in out.output and "tenants" in out.output


def test_doctor_cli_setup_requests_default_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _report(("db", "pass")))
    monkeypatch.setattr(doctor_cli, "request_default_grants", lambda cfg: ["g1", "g2", "g3"])
    out = CliRunner().invoke(app, ["doctor", "--setup"])
    assert out.exit_code == 0
    assert "requested 3 default T1 grant(s)" in out.output


def test_doctor_cli_setup_skipped_when_db_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _report(("db", "fail")))
    called: list[Any] = []
    monkeypatch.setattr(doctor_cli, "request_default_grants", called.append)
    out = CliRunner().invoke(app, ["doctor", "--setup", "--json"])
    assert out.exit_code == 1
    assert json.loads(out.output)["setup"] == "skipped: the db check did not pass"
    assert called == []
