"""ADR-0021 §4: ``records.query``, a reviewed SELECT whose only inputs are bound parameters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.records import RecordsQueryTool, prepare
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors

pytestmark = pytest.mark.unit


class _Reader:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str, dict[str, str], int]] = []

    def records(
        self,
        database: str,
        sql: str,
        params: Mapping[str, str],
        *,
        limit: int,
        timeout_s: float,
    ) -> tuple[list[dict[str, object]], bool]:
        self.calls.append((database, sql, dict(params), limit))
        return self.rows[:limit], len(self.rows) > limit


def _run(args: dict[str, object], rows: list[dict[str, object]] | None = None) -> Any:
    reader = _Reader(rows or [])
    result = RecordsQueryTool(reader=reader).run({"database": "orders", **args}, timeout_s=5)
    return result, reader


def test_the_id_is_a_bound_parameter_never_part_of_the_statement() -> None:
    sql = "SELECT status FROM oms_orders WHERE intent_id::text = :id"
    hostile = "x' OR '1'='1"
    _, reader = _run({"sql": sql, "id": hostile})
    database, sent, params, _ = reader.calls[0]
    assert database == "orders"
    assert sent == "SELECT status FROM oms_orders WHERE intent_id::text = %(id)s"
    assert params == {"id": hostile}


def test_percent_is_literal_and_casts_are_left_alone() -> None:
    sql, params = prepare(
        "SELECT ts::date, count(*) FROM bars WHERE symbol LIKE 'BTC%' AND ref = :key;",
        {"key": "k-1"},
    )
    assert sql == "SELECT ts::date, count(*) FROM bars WHERE symbol LIKE 'BTC%%' AND ref = %(key)s"
    assert params == {"key": "k-1"}


@pytest.mark.parametrize(
    ("sql", "args", "error"),
    [
        ("DELETE FROM oms_orders", {}, "only SELECT"),
        ("SELECT 1; DROP TABLE oms_orders", {}, "one statement"),
        ("SELECT * FROM t WHERE id = :id", {}, ":id, which isn't given"),
        ("SELECT 1", {"id": "o-1"}, "id given but not used"),
        ("", {}, "a statement of"),
    ],
)
def test_only_one_reviewed_select_with_its_parameters(
    sql: str, args: dict[str, object], error: str
) -> None:
    with pytest.raises(ToolError, match=error):
        prepare(sql, args)


def test_needs_a_bound_database_and_a_bounded_limit() -> None:
    tool = RecordsQueryTool(reader=_Reader([]))
    with pytest.raises(ToolError, match=r"\$service.databases"):
        tool.run({"sql": "SELECT 1"}, timeout_s=5)
    for limit in (0, 51, "many"):
        with pytest.raises(ToolError, match="limit"):
            tool.run({"database": "orders", "sql": "SELECT 1", "limit": limit}, timeout_s=5)


def test_rows_are_json_scalars_capped_and_match_the_schema() -> None:
    row: dict[str, object] = {
        "id": UUID("6b1f3c2e-0000-4000-8000-000000000001"),
        "qty": Decimal("0.25"),
        "at": datetime(2026, 11, 8, 1, 3, tzinfo=UTC),
        "note": "x" * 900,
        "raw": b"\x00\x01",
        "ok": True,
        "missing": None,
    }
    result, _ = _run({"sql": "SELECT * FROM oms_orders", "limit": 2}, [row, row, row])
    assert result.truncated
    data = dict(result.data)
    assert data["count"] == 2
    first = data["rows"][0]
    assert first["id"] == "6b1f3c2e-0000-4000-8000-000000000001"
    assert first["qty"] == 0.25
    assert first["at"] == "2026-11-08T01:03:00+00:00"
    assert len(first["note"]) == 500
    assert first["raw"] == "<2 bytes>"
    assert (first["ok"], first["missing"]) == (True, None)
    assert schema_errors(data, RecordsQueryTool.result_schema) == []


def test_records_query_is_a_capability_with_literal_sql() -> None:
    capability = CAPABILITIES["records.query"]
    assert {"sql", "id", "key", "limit"} == capability.literal_args
    assert "database" not in capability.literal_args  # bound from the catalog only


def test_dsn_read_from_its_env(tmp_path: Path) -> None:
    from supportagent.config.loader import load_config  # noqa: PLC0415

    repo = Path(__file__).resolve().parents[2]
    raw = (repo / "supportagent.example.yaml").read_text(encoding="utf-8")
    raw += "tools:\n  records:\n    databases:\n      orders: {dsn_env: PG_ORDERS_RECORDS}\n"
    path = tmp_path / "c.yaml"
    path.write_text(raw, encoding="utf-8")
    env = {
        "SUPPORTAGENT_DB_URL": "sqlite://",
        "SN_CLIENT_ID": "x",
        "SN_CLIENT_SECRET": "x",
        "OIDC_CLIENT_ID": "x",
        "OIDC_CLIENT_SECRET": "x",
        "SA_SESSION_SECRET": "x" * 40,
        "ANTHROPIC_API_KEY": "x",
        "PAGERDUTY_TOKEN": "x",
        "PG_ORDERS_RECORDS": "postgresql://records@db/orders",
    }
    config = load_config(path, env)
    assert config.tools.records is not None
    dsn = config.tools.records.databases["orders"].dsn.get_secret_value()
    assert dsn == "postgresql://records@db/orders"


def test_registered_only_when_configured() -> None:
    from supportagent.adapters.fakes.clock import FrozenClock  # noqa: PLC0415
    from supportagent.adapters.fakes.events import RecordingEventSink  # noqa: PLC0415
    from supportagent.adapters.fakes.ids import SequentialIdGenerator  # noqa: PLC0415
    from supportagent.bootstrap import build_tool_registry  # noqa: PLC0415
    from supportagent.config.schema import Tools  # noqa: PLC0415
    from tests.unit.test_triage_service import _shadow_config  # noqa: PLC0415

    base = _shadow_config()
    clock = FrozenClock(start=datetime(2026, 11, 8, tzinfo=UTC))

    def names(tools: Tools) -> frozenset[str]:
        config = base.model_copy(update={"tools": tools})
        return build_tool_registry(
            config, events=RecordingEventSink(), ids=SequentialIdGenerator(), clock=clock
        ).names

    configured = Tools.model_validate(
        {"records": {"databases": {"orders": {"dsn": "postgresql://records@db/orders"}}}}
    )
    assert "records.query" in names(configured)
    assert "records.query" not in names(base.tools)
