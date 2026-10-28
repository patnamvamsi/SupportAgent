"""WP-115: promotion gate, approval fatigue, quickstart (CAP §4.7, ARCH §8.4).

Canonical test IDs from 11-wp §WP-115: ``test_promotion_gate_uses_lower_bound``,
``test_promotion_gate_per_severity_band``, ``test_approval_fatigue_metrics``,
``test_quickstart_commands_valid``.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.gates_loader import load_promotion_gate
from supportagent.app.stats import StatsService
from supportagent.domain.errors import ConfigError
from supportagent.domain.promotion import PromotionGate, Threshold, evaluate
from supportagent.entrypoints.cli.main import app
from tests.unit.api_support import ADA, TENANT, login, make_client, seed_proposal, seed_users
from tests.unit.test_stats import _shadow

_REPO = Path(__file__).resolve().parents[2]
GATE = PromotionGate()


def test_promotion_gate_uses_lower_bound() -> None:
    # CAP §4.7: 80 % over 30 cases (lower bound ~0.63) does not pass.
    small = evaluate((24, 30), {"P1-P2": (20, 20)}, GATE)
    assert not small.passed
    assert "needs >= 60" in small.checks[0].reason
    # 80 % over 60 still fails: the lower bound (~0.68) is what counts.
    rate_only = evaluate((48, 60), {"P1-P2": (20, 20)}, GATE)
    assert not rate_only.passed
    assert rate_only.checks[0].lower_bound is not None and rate_only.checks[0].lower_bound < 0.75
    strong = evaluate((57, 60), {"P1-P2": (20, 20)}, GATE)
    assert strong.passed and strong.provisional


def test_promotion_gate_per_severity_band() -> None:
    overall_ok = (57, 60)
    weak_band = evaluate(overall_ok, {"P1-P2": (12, 20)}, GATE)
    assert not weak_band.passed, "a strong overall number can't hide a weak P1/P2 band"
    assert [c.name for c in weak_band.checks if not c.passed] == ["P1-P2"]
    missing = evaluate(overall_ok, {}, GATE)
    assert not missing.passed and "needs >= 20" in missing.checks[1].reason


def test_gates_file_loads_and_validates(tmp_path: Path) -> None:
    gate = load_promotion_gate(_REPO / "evals" / "gates.yaml")
    assert gate.overall == Threshold(0.75, 60) and gate.provisional
    bad = tmp_path / "gates.yaml"
    bad.write_text(
        "promotion:\n  overall: {min_lower_bound: 1.5, min_cases: 60}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_promotion_gate(bad)


def test_stats_promotion_from_shadow_records(db: Any, clock: Any) -> None:
    for i in range(60):
        _shadow(db, f"s{i}", agreement=1 if i < 57 else 0, human_severity="P1" if i < 20 else "P3")
    stats = StatsService(uow=SqlUnitOfWork(db), clock=clock, tenant_id="acme")
    result = stats.promotion(GATE)
    assert [(c.name, c.n, c.agreed) for c in result.checks] == [
        ("overall", 60, 57),
        ("P1-P2", 20, 20),
    ]
    assert result.passed


def _approval(  # noqa: PLR0913
    db: Any,
    pid: str,
    *,
    created: int,
    decided: int,
    status: str,
    reason: str | None,
) -> None:
    seed_proposal(
        db, proposal_id=pid, case_id=f"case_{pid}", created_at_ts=created, external_id=f"INC_{pid}"
    )
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO approvals (id, tenant_id, proposal_id, approver_id, status, "
                "payload_hash, reason, approved_at_ts) SELECT :id, :t, p.id, 'sub-bob', :st, "
                "p.payload_hash, :r, :ts FROM proposals p WHERE p.id = :pid"
            ),
            {
                "id": f"appr_{pid}",
                "t": TENANT,
                "st": status,
                "r": reason,
                "ts": decided,
                "pid": pid,
            },
        )


def test_approval_fatigue_metrics(db: Any, clock: Any) -> None:
    seed_users(db, ("sub-bob", "approver"))
    waits = [30, 60, 120, 600, 3600]
    for i, wait in enumerate(waits):
        _approval(
            db,
            f"p{i}",
            created=1_000,
            decided=1_000 + wait,
            status="approved",
            reason=None if i < 3 else "checked the graph",
        )
    _approval(db, "p_rej", created=1_000, decided=1_100, status="rejected", reason="wrong")
    fatigue = StatsService(uow=SqlUnitOfWork(db), clock=clock, tenant_id="acme").fatigue()
    assert (fatigue.approvals, fatigue.rejections) == (5, 1)
    assert (fatigue.approve_time_p50_s, fatigue.approve_time_p90_s) == (120, 3600)
    assert fatigue.one_click_rate == pytest.approx(3 / 5)


def test_promotion_api(db: Any, clock: Any, ids: Any, master_key: bytes) -> None:
    oidc = FakeOIDC()
    client: TestClient = make_client(db, clock, ids, master_key, oidc)
    login(client, oidc, ADA)
    body = client.get("/api/v1/stats/promotion").json()
    assert body["promotion"]["passed"] is False and body["promotion"]["provisional"] is True
    assert body["approval_fatigue"]["approvals"] == 0
    page = client.get("/ui/stats").text
    assert "Promotion gate" in page and "Approval fatigue" in page


# --- quickstart -------------------------------------------------------------

_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: Plain, wide help output on every terminal (CI renders Rich colour otherwise).
_PLAIN = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200", "FORCE_COLOR": ""}


def _commands() -> list[list[str]]:
    doc = (_REPO / "docs" / "quickstart.md").read_text(encoding="utf-8")
    out: list[list[str]] = []
    for block in _BLOCK.findall(doc):
        for line in block.splitlines():
            if line.strip().startswith("supportagent"):
                out.append(shlex.split(line.strip())[1:])
    return out


def test_quickstart_commands_valid() -> None:
    """Every ``supportagent …`` line in the quickstart names a real command and real flags.

    11-wp WP-115 runs the quickstart against compose in e2e. Here every
    command is checked against the CLI itself, so a renamed flag or command
    fails CI before a reader hits it.
    """
    commands = _commands()
    assert len(commands) >= 8, "the quickstart covers install to stats"
    runner = CliRunner()
    for args in commands:
        if args == ["--version"]:
            assert runner.invoke(app, ["--version"]).exit_code == 0
            continue
        path = [a for a in args if not a.startswith("-")]
        # Walk to the deepest subcommand that exists (positional args follow it).
        depth = len(path)
        while depth and runner.invoke(app, [*path[:depth], "--help"], env=_PLAIN).exit_code:
            depth -= 1
        assert depth, f"unknown command: supportagent {' '.join(args)}"
        help_text = _ANSI.sub("", runner.invoke(app, [*path[:depth], "--help"], env=_PLAIN).output)
        for flag in (a.split("=", 1)[0] for a in args if a.startswith("--")):
            assert flag in help_text, f"supportagent {' '.join(path[:depth])}: no {flag}"
