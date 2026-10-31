"""WP-O6a: the oracle plugin's read-only tools, on a fake reader; and the new pod fields."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from supportagent.adapters.tools import oracle
from supportagent.adapters.tools.kubernetes import PodStatusTool
from supportagent.adapters.tools.oracle import (
    ALL_SQL,
    LOCKS_SQL,
    SESSIONS_SQL,
    TABLESPACES_SQL,
    TOP_SQL_SQL,
    WAITS_SQL,
    OracledbReader,
    OracleLocksTool,
    OracleSessionsTool,
    OracleTablespacesTool,
    OracleTopSqlTool,
)
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors

pytestmark = pytest.mark.unit

_DB = {"database": "paydb"}


class _Reader:
    def __init__(self, rows: Mapping[str, list[dict[str, object]]]) -> None:
        self._rows = rows
        self.seen: list[tuple[str, str]] = []

    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]:
        self.seen.append((database, sql))
        return list(self._rows.get(sql, []))


def _run(tool_cls: Any, rows: Mapping[str, list[dict[str, object]]]) -> dict[str, Any]:
    reader = _Reader(rows)
    tool = tool_cls(reader=reader)
    data = dict(tool.run(_DB, timeout_s=5).data)
    assert schema_errors(data, tool.result_schema) == []
    assert {db for db, _ in reader.seen} == {"paydb"}
    return data


def test_every_oracle_statement_is_a_constant_select() -> None:
    for sql in ALL_SQL:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), sql
        assert ":" not in sql  # no bind variables: nothing from a case reaches the SQL


def test_the_reader_refuses_writes_and_unknown_databases() -> None:
    reader = OracledbReader({"paydb": "ro/x@db/pay"})
    with pytest.raises(ToolError, match="only SELECT"):
        reader.query("paydb", "DELETE FROM payments", timeout_s=1)
    with pytest.raises(ToolError, match="not configured"):
        reader.query("crmdb", "SELECT 1 FROM dual", timeout_s=1)


def test_the_reader_says_when_the_driver_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ToolError, match="python-oracledb is not installed"):
        OracledbReader({"paydb": "ro/x@db/pay"}).query("paydb", "SELECT 1 FROM dual", timeout_s=1)


class _Cursor:
    description = (("TOTAL",), ("ACTIVE",))

    def __init__(self, log: list[str]) -> None:
        self._log = log

    def execute(self, sql: str) -> None:
        self._log.append(sql.strip().split()[0] + (" READ ONLY" if "READ ONLY" in sql else ""))

    def fetchmany(self, n: int) -> list[tuple[object, ...]]:
        return [(12, 3)]


class _Conn:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.call_timeout = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self._log)

    def rollback(self) -> None:
        self._log.append("ROLLBACK")

    def close(self) -> None:
        self._log.append("CLOSE")


def test_the_reader_runs_each_statement_read_only_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []

    class _Driver:
        Error = RuntimeError

        @staticmethod
        def connect(dsn: str) -> _Conn:
            assert dsn == "ro/x@db/pay"
            return _Conn(log)

    monkeypatch.setattr(importlib, "import_module", lambda name: _Driver)
    rows = OracledbReader({"paydb": "ro/x@db/pay"}, call_timeout_ms=800).query(
        "paydb", SESSIONS_SQL, timeout_s=5
    )
    assert rows == [{"total": 12, "active": 3}]
    assert log == ["SET READ ONLY", "SELECT", "ROLLBACK", "CLOSE"]


def test_sessions_count_and_top_waits() -> None:
    data = _run(
        OracleSessionsTool,
        {
            SESSIONS_SQL: [{"total": 310, "active": 42, "blocked": 27}],
            WAITS_SQL: [{"event": "enq: TX - row lock contention", "sessions": 27}],
        },
    )
    assert (data["total"], data["active"], data["blocked"]) == (310, 42, 27)
    assert data["top_waits"] == [{"event": "enq: TX - row lock contention", "sessions": 27}]


def test_locks_group_waiters_by_blocker_with_the_longest_wait() -> None:
    def row(waiter: int, blocker: int, wait: float) -> dict[str, object]:
        return {
            "waiter_sid": waiter,
            "blocker_sid": blocker,
            "event": "enq: TX - row lock contention",
            "wait_s": wait,
            "blocker_status": "INACTIVE",
            "blocker_sql_id": None,
        }

    data = _run(OracleLocksTool, {LOCKS_SQL: [row(10, 7, 30), row(11, 7, 912), row(12, 8, 3)]})
    assert data["blocked"] == 3
    first = data["blockers"][0]
    assert (first["sid"], first["blocking"], first["wait_s"]) == (7, 2, 912.0)


def test_top_sql_finds_the_statement_slower_on_its_newest_plan() -> None:
    data = _run(
        OracleTopSqlTool,
        {
            TOP_SQL_SQL: [
                {
                    "sql_id": "3k9d",
                    "plan_hash_value": 2,
                    "executions": 10,
                    "elapsed_us": 69_000_000,
                    "last_active": "2026-10-30T09:00:00",
                },
                {
                    "sql_id": "3k9d",
                    "plan_hash_value": 1,
                    "executions": 1000,
                    "elapsed_us": 140_000_000,
                    "last_active": "2026-10-29T23:00:00",
                },
                {
                    "sql_id": "aa01",
                    "plan_hash_value": 5,
                    "executions": 50,
                    "elapsed_us": 900_000_000,
                    "last_active": "2026-10-30T09:00:00",
                },
            ]
        },
    )
    regressed, heavy = data["statements"]
    assert regressed["sql_id"] == "3k9d" and regressed["plan_changed"]
    assert regressed["mean_ms"] == 6900.0 and regressed["previous_mean_ms"] == 140.0
    assert data["max_slowdown"] == regressed["slowdown"] == pytest.approx(49.286, abs=0.001)
    assert heavy == {
        "sql_id": "aa01",
        "plans": 1,
        "mean_ms": 18000.0,
        "previous_mean_ms": None,
        "plan_changed": False,
        "slowdown": 1.0,
    }


def test_a_faster_new_plan_is_not_a_regression() -> None:
    rows: list[dict[str, object]] = [
        {"sql_id": "x", "plan_hash_value": n, "executions": 1, "elapsed_us": us, "last_active": t}
        for n, us, t in ((1, 9e6, "1"), (2, 1e6, "2"))
    ]
    data = _run(OracleTopSqlTool, {TOP_SQL_SQL: rows})
    assert data["statements"][0]["plan_changed"] and data["max_slowdown"] < 1


def test_tablespaces_report_the_fullest() -> None:
    data = _run(
        OracleTablespacesTool,
        {
            TABLESPACES_SQL: [
                {"tablespace_name": "SETTLEMENT_DATA", "used_percent": 100.0},
                {"tablespace_name": "USERS", "used_percent": 12.345},
            ]
        },
    )
    assert data["max_used_pct"] == 100.0
    assert data["tablespaces"][1] == {"name": "USERS", "used_pct": 12.35}


def test_an_oracle_tool_needs_a_bound_database() -> None:
    with pytest.raises(ToolError, match=r"\$service.databases"):
        OracleLocksTool(reader=_Reader({})).run({}, timeout_s=1)


def test_the_tools_are_in_the_capability_catalog() -> None:
    from supportagent.adapters.tools.capabilities import CAPABILITIES  # noqa: PLC0415

    assert {t.name for t in oracle.ORACLE_TOOLS} <= set(CAPABILITIES)


def test_pod_status_reports_evictions_and_image_pulls() -> None:
    items = [
        {
            "metadata": {"name": "a"},
            "status": {"phase": "Failed", "reason": "Evicted", "containerStatuses": []},
        },
        {
            "metadata": {"name": "b"},
            "status": {
                "phase": "Pending",
                "containerStatuses": [
                    {
                        "ready": False,
                        "restartCount": 0,
                        "state": {"waiting": {"reason": "ImagePullBackOff"}},
                    }
                ],
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": items})

    client = httpx.Client(base_url="http://k", transport=httpx.MockTransport(handler))
    tool = PodStatusTool(client=client)
    data: dict[str, Any] = dict(
        tool.run({"namespace": "shop", "selector": "app=x"}, timeout_s=5).data
    )
    assert schema_errors(data, tool.result_schema) == []
    evicted, pulling = data["pods"]
    assert (evicted["reason"], evicted["waiting"]) == ("Evicted", None)
    assert (pulling["reason"], pulling["waiting"]) == (None, "ImagePullBackOff")
