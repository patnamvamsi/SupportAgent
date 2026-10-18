"""INV-03: the event log is append-only; replay is ordered and decrypted.

Frozen test IDs (10-wp WP-004, L83):

- ``test_events_append_only``
- ``test_event_requires_payload_version``
- ``test_event_rolls_back_with_uow``
- ``test_replay_orders_by_time_and_span``
- ``test_replay_unknown_trace_exit_code_4``
- ``test_trace_child_span_parent_link``

plus the WP-004 DoD anchor: ``supportagent replay`` works on a seeded trace
(02 §4), at the CLI level (table and ``--json``).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from typer.testing import CliRunner

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.engine import make_engine
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.system.clock import SystemClock
from supportagent.app.replay import ReplayService, summarize
from supportagent.bootstrap import run_migrations
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event
from supportagent.domain.trace import TraceContext
from supportagent.entrypoints.cli.main import app
from tests.builders import SECRET_ENV, T0, a_config_file
from tests.yaml_io import write_yaml_file

# 03 §3 master key for the CLI tests: 64 hex chars (32 bytes, AES-256-GCM).
# Low-entropy by design — a fake test value; a high-entropy literal would
# trip the gitleaks generic-api-key rule in CI (see tests/builders.py).
_MASTER_KEY_HEX = "00" * 32


def _event(**overrides: object) -> dict[str, Any]:
    """A valid event (01 §4) with one field overridable at a time."""
    base: dict[str, Any] = {
        "id": "evt_0001",
        "tenant_id": "acme",
        "trace_id": "trace_0001",
        "span_id": "span_0001",
        "parent_span_id": None,
        "kind": EventKind.INGEST,
        "actor": "system",
        "model": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 0,
        "status": "ok",
        "payload": {"v": 1, "origin": "alert", "source": "servicenow"},
        "ts": T0,
    }
    base.update(overrides)
    return base


def test_events_append_only(db: Any, uow: Any, aead: Any) -> None:
    """INV-03: UPDATE on the events table is aborted by the trigger (03 §2.4)."""
    with uow:
        SqlEventSink(uow=uow, cipher=aead).append(Event(**_event()))
        uow.commit()
    with (
        pytest.raises(sa.exc.DBAPIError, match="INV-03: events are append-only"),
        db.connect() as conn,
    ):
        conn.exec_driver_sql("UPDATE events SET actor = 'hacker' WHERE id = 'evt_0001'")


def test_event_requires_payload_version() -> None:
    """10-wp WP-004: a missing 'v' is a DomainValidationError (01 §7.1)."""
    with pytest.raises(DomainValidationError, match=r"payload must contain 'v'"):
        Event(**_event(payload={}))


def test_event_rolls_back_with_uow(db: Any, uow: Any, aead: Any) -> None:
    """01 §3: append happens in the caller's UoW — a failed UoW loses the row."""
    with pytest.raises(RuntimeError, match="boom"), uow:
        SqlEventSink(uow=uow, cipher=aead).append(Event(**_event()))
        raise RuntimeError("boom")
    with db.connect() as conn:
        assert int(conn.execute(sa.text("SELECT COUNT(*) FROM events")).scalar_one()) == 0


def test_replay_orders_by_time_and_span(db: Any, uow: Any, aead: Any) -> None:
    """01 §5: replay returns the trace's events ordered by (ts_ms, span_id).

    Insertion order is deliberately not display order: the late span lands
    last in the log, but three same-millisecond spans must come back sorted
    by span id. Payloads round-trip decrypted.
    """
    early = [
        ("evt_0003", "span_z", {"v": 1, "step": 3}),
        ("evt_0001", "span_a", {"v": 1, "step": 1}),
        ("evt_0002", "span_b", {"v": 1, "step": 2}),
    ]
    with uow:
        sink = SqlEventSink(uow=uow, cipher=aead)
        for event_id, span_id, payload in early:
            sink.append(Event(**_event(id=event_id, span_id=span_id, payload=payload)))
        sink.append(
            Event(**_event(id="evt_0004", span_id="span_late", ts=T0 + timedelta(seconds=2)))
        )
        uow.commit()
    with uow:
        view = ReplayService(repo=SqlEventSink(uow=uow, cipher=aead)).replay("trace_0001")

    assert [e.span_id for e in view.events] == ["span_a", "span_b", "span_z", "span_late"]
    assert [e.id for e in view.events] == ["evt_0001", "evt_0002", "evt_0003", "evt_0004"]
    # Decrypted payload round-trips; the summary registry is per-kind (10-wp).
    assert view.events[0].payload == {"v": 1, "step": 1}
    assert summarize(view.events[0].kind, view.events[0].payload)


def test_replay_unknown_trace_exit_code_4(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """02 §4: ``supportagent replay <unknown>`` exits 4 (not found)."""
    db_file = tmp_path / "cli.db"
    url = f"sqlite:///{db_file}"
    run_migrations(url)  # empty, migrated log — nothing to replay
    config_path = write_yaml_file(tmp_path, "supportagent.yaml", a_config_file())

    env = dict(SECRET_ENV)
    env["SUPPORTAGENT_DB_URL"] = url
    env["SA_MASTER_KEY_ENV"] = _MASTER_KEY_HEX
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result = CliRunner().invoke(app, ["replay", "trace_missing", "--config", str(config_path)])
    assert result.exit_code == 4
    assert "not found" in result.output.lower()


def test_replay_cli_seeded_trace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """WP-004 DoD: ``supportagent replay`` works on a seeded trace (02 §4)."""
    db_file = tmp_path / "cli.db"
    url = f"sqlite:///{db_file}"
    run_migrations(url)
    engine = make_engine(url)
    with SqlUnitOfWork(engine) as uow:
        aead = Aead(
            master_key=bytes.fromhex(_MASTER_KEY_HEX),
            keys=SqlKeyStore(uow),
            clock=SystemClock(),
        )
        SqlEventSink(uow=uow, cipher=aead).append(Event(**_event()))
        uow.commit()
    engine.dispose()

    config_path = write_yaml_file(tmp_path, "supportagent.yaml", a_config_file())
    env = dict(SECRET_ENV)
    env["SUPPORTAGENT_DB_URL"] = url
    env["SA_MASTER_KEY_ENV"] = _MASTER_KEY_HEX
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    args = ["replay", "trace_0001", "--config", str(config_path)]
    table = CliRunner().invoke(app, args)
    assert table.exit_code == 0
    assert "ingest" in table.output
    assert "origin=alert" in table.output  # the per-kind one-line summary (10-wp)

    as_json = CliRunner().invoke(app, [*args, "--json"])
    assert as_json.exit_code == 0
    data = json.loads(as_json.output)
    assert data["trace_id"] == "trace_0001"
    assert data["tenant_id"] == "acme"
    row = data["events"][0]
    assert row["kind"] == "ingest"
    assert row["status"] == "ok"
    assert row["tokens"] == {"in": 0, "out": 0}
    assert row["summary"] == "origin=alert source=servicenow"


def test_trace_child_span_parent_link() -> None:
    """01 §4: ``child()`` keeps tenant + trace and links the parent span."""
    root = TraceContext.new("acme", "trace_0001", "span_0001")
    assert root.parent_span_id is None
    child = root.child("span_0002")
    assert child.tenant_id == "acme"
    assert child.trace_id == "trace_0001"
    assert child.span_id == "span_0002"
    assert child.parent_span_id == "span_0001"
