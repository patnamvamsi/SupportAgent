"""WP-014: ``supportagent stats`` and ``supportagent shadow import`` wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from supportagent.app.stats import AgreementStat, LatencyStat, StatsReport
from supportagent.domain.model.shadow import ShadowRecord
from supportagent.entrypoints.cli import shadow as shadow_cli
from supportagent.entrypoints.cli import stats as stats_cli
from supportagent.entrypoints.cli.main import app


def _report(mode: str) -> StatsReport:
    return StatsReport(
        mode=mode,  # type: ignore[arg-type]
        window_days=30.0,
        n_cases=3,
        agreement=AgreementStat.of([True, True, False]),
        agreement_by_band={"P1-P2": AgreementStat.of([]), "P3-P4": AgreementStat.of([True])},
        first_proposal_latency_s=LatencyStat(n=2, p50=4, p95=9),
        safe_default_by_reason={"llm-failure": 1},
        safe_default_rate=1 / 3,
    )


class _Stats:
    def __init__(self) -> None:
        self.calls: list[tuple[timedelta | None, str]] = []

    def report(self, *, window: timedelta | None, mode: str) -> StatsReport:
        self.calls.append((window, mode))
        return _report(mode)


def test_stats_cli_json_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Stats()
    monkeypatch.setattr(stats_cli, "load_config_default", lambda path: "cfg")
    monkeypatch.setattr(stats_cli, "build_stats", lambda cfg: fake)
    out = CliRunner().invoke(app, ["stats", "--mode", "assisted", "--window", "7d", "--json"])
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["mode"] == "assisted"
    assert fake.calls == [(timedelta(days=7), "assisted")]
    table = CliRunner().invoke(app, ["stats"])
    assert table.exit_code == 0
    assert "agreement  2/3" in table.output
    assert "llm-failure" in table.output


def test_stats_cli_bad_window_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stats_cli, "load_config_default", lambda path: "cfg")
    monkeypatch.setattr(stats_cli, "build_stats", lambda cfg: _Stats())
    out = CliRunner().invoke(app, ["stats", "--window", "3w"])
    assert out.exit_code == 2


def test_shadow_import_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    when = datetime(2026, 1, 1, tzinfo=UTC)

    def rec(i: int, agree: bool | None) -> ShadowRecord:
        return ShadowRecord(
            id=f"shdw_{i}",
            tenant_id="acme",
            case_id=f"case_{i}",
            alert_received_at=when,
            first_proposal_at=None,
            proposed={},
            agreement=agree,
        )

    seen: list[Path] = []

    def fake_import(_cfg: Any, path: Path) -> list[ShadowRecord]:
        seen.append(path)
        return [rec(1, True), rec(2, False), rec(3, None)]

    monkeypatch.setattr(shadow_cli, "load_config_default", lambda path: "cfg")
    monkeypatch.setattr(shadow_cli, "shadow_import", fake_import)
    export = tmp_path / "x.jsonl"
    out = CliRunner().invoke(app, ["shadow", "import", str(export)])
    assert out.exit_code == 0, out.output
    assert "replayed 3 incident(s); 1/2 labelled agree" in out.output
    assert seen == [export]
