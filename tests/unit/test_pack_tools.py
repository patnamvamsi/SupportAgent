"""WP-M04: the python-postgres pack's read-only tools, on fakes and mocked HTTP."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from supportagent.adapters.tools.changes import RecentChangesTool
from supportagent.adapters.tools.kubernetes import PodStatusTool
from supportagent.adapters.tools.postgres import (
    ACTIVITY_SQL,
    ALL_SQL,
    LOCKS_SQL,
    MAX_CONNECTIONS_SQL,
    RECOVERY_SQL,
    REPLICAS_SQL,
    SIZE_SQL,
    SLOW_SQL,
    STATEMENTS_EXT_SQL,
    TABLES_SQL,
    PgActivityTool,
    PgLocksTool,
    PgReplicationTool,
    PgSlowQueriesTool,
    PgStorageTool,
    PsycopgReader,
)
from supportagent.adapters.tools.prometheus import PrometheusQueryTool
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return NOW


class _Reader:
    """A ``PgReader`` answering each fixed statement with canned rows."""

    def __init__(self, rows: Mapping[str, list[dict[str, object]]]) -> None:
        self._rows = rows
        self.seen: list[tuple[str, str]] = []

    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]:
        self.seen.append((database, sql))
        return list(self._rows.get(sql, []))


def _valid(tool: Any, data: Mapping[str, object]) -> None:
    assert schema_errors(dict(data), tool.result_schema) == []


# --- postgres --------------------------------------------------------------------------


def test_every_postgres_statement_is_read_only() -> None:
    for sql in ALL_SQL:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), sql


def test_postgres_tool_rejects_non_select() -> None:
    reader = PsycopgReader({"orders": "postgresql://ro@localhost/orders"})
    with pytest.raises(ToolError, match="only SELECT"):
        reader.query("orders", "DELETE FROM orders", timeout_s=1)


def test_postgres_tool_requires_a_configured_database() -> None:
    reader = PsycopgReader({"orders": "postgresql://ro@localhost/orders"})
    with pytest.raises(ToolError, match="not configured"):
        reader.query("billing", "SELECT 1", timeout_s=1)


def test_postgres_activity_reports_pool_pressure() -> None:
    reader = _Reader(
        {
            ACTIVITY_SQL: [
                {"state": "active", "n": 180, "waiting": 12, "oldest_xact_s": 4.5},
                {"state": "idle in transaction", "n": 310, "waiting": 0, "oldest_xact_s": 900.0},
                {"state": "idle", "n": 8, "waiting": 0, "oldest_xact_s": 0},
            ],
            MAX_CONNECTIONS_SQL: [{"max_connections": 500}],
        }
    )
    result = PgActivityTool(reader=reader).run({"database": "orders"}, timeout_s=5).data
    _valid(PgActivityTool, result)
    assert result["total"] == 498 and result["max_connections"] == 500
    assert result["idle_in_transaction"] == 310
    assert result["waiting"] == 12 and result["oldest_xact_age_s"] == 900.0
    assert {db for db, _ in reader.seen} == {"orders"}


def test_postgres_locks_groups_by_blocker() -> None:
    reader = _Reader(
        {
            LOCKS_SQL: [
                {
                    "blocked_pid": 11,
                    "blocker_pid": 7,
                    "blocker_state": "idle in transaction",
                    "blocker_xact_s": 620.0,
                },
                {
                    "blocked_pid": 12,
                    "blocker_pid": 7,
                    "blocker_state": "idle in transaction",
                    "blocker_xact_s": 620.0,
                },
            ]
        }
    )
    result = PgLocksTool(reader=reader).run({"database": "orders"}, timeout_s=5).data
    _valid(PgLocksTool, result)
    assert result["blocked"] == 2
    assert result["blockers"] == [
        {"pid": 7, "blocking": 2, "state": "idle in transaction", "xact_age_s": 620.0}
    ]


def test_postgres_slow_queries_without_the_extension() -> None:
    result = (
        PgSlowQueriesTool(reader=_Reader({STATEMENTS_EXT_SQL: [{"n": 0}]}))
        .run({"database": "orders"}, timeout_s=5)
        .data
    )
    _valid(PgSlowQueriesTool, result)
    assert result == {"database": "orders", "available": False, "queries": []}


def test_postgres_slow_queries_top_by_mean() -> None:
    reader = _Reader(
        {
            STATEMENTS_EXT_SQL: [{"n": 1}],
            SLOW_SQL: [
                {
                    "queryid": 42,
                    "calls": 900,
                    "mean_ms": 1250.5,
                    "total_ms": 1125450.0,
                    "query": "SELECT * FROM orders WHERE customer_id = $1",
                },
            ],
        }
    )
    result = PgSlowQueriesTool(reader=reader).run({"database": "orders"}, timeout_s=5).data
    _valid(PgSlowQueriesTool, result)
    assert result["available"] is True and result["queries"][0]["mean_ms"] == 1250.5  # type: ignore[index]


def test_postgres_replication_on_a_primary() -> None:
    reader = _Reader(
        {
            RECOVERY_SQL: [{"is_replica": False}],
            REPLICAS_SQL: [{"name": "replica-1", "state": "streaming", "lag_s": 42.0}],
        }
    )
    result = PgReplicationTool(reader=reader).run({"database": "orders"}, timeout_s=5).data
    _valid(PgReplicationTool, result)
    assert result["max_lag_s"] == 42.0 and not result["is_replica"]


def test_postgres_storage_dead_tuple_ratio() -> None:
    reader = _Reader(
        {
            SIZE_SQL: [{"size_bytes": 10_000_000}],
            TABLES_SQL: [{"name": "orders", "live": 1000, "dead": 3000, "last_autovacuum": None}],
        }
    )
    result = PgStorageTool(reader=reader).run({"database": "orders"}, timeout_s=5).data
    _valid(PgStorageTool, result)
    assert result["max_dead_ratio"] == 0.75


def test_postgres_tool_needs_a_bound_database() -> None:
    with pytest.raises(ToolError, match="databases"):
        PgActivityTool(reader=_Reader({})).run({}, timeout_s=5)


# --- metrics.query ------------------------------------------------------------------------


def _client(handler: Any, base: str = "http://x") -> httpx.Client:
    return httpx.Client(base_url=base, transport=httpx.MockTransport(handler))


def test_metrics_query_schema() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"pod": "a"}, "value": [1, "0.05"]},
                        {"metric": {"pod": "b"}, "value": [1, "0.31"]},
                        {"metric": {"pod": "c"}, "value": [1, "NaN"]},
                    ],
                },
            },
        )

    tool = PrometheusQueryTool(client=_client(handler))
    query = 'sum by (pod) (rate(http_requests_total{service="{{service}}",code=~"5.."}[5m]))'
    result = tool.run({"query": query, "service": "checkout-api"}, timeout_s=5).data
    _valid(tool, result)
    assert seen == [query.replace("{{service}}", "checkout-api")]
    assert result["count"] == 2 and result["max_value"] == 0.31


def test_metrics_query_refuses_an_unbound_service_placeholder() -> None:
    tool = PrometheusQueryTool(client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ToolError, match="service"):
        tool.run(
            {"query": 'up{service="{{service}}"}', "service": '"} or vector(1) #'}, timeout_s=5
        )


# --- k8s.pod_status --------------------------------------------------------------------------


def _pod(name: str, *, ready: bool, restarts: int, reason: str | None) -> dict[str, Any]:
    last = {"terminated": {"reason": reason}} if reason else {}
    return {
        "metadata": {"name": name},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"ready": ready, "restartCount": restarts, "lastState": last}],
        },
    }


def test_pod_status_reports_oomkilled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/namespaces/shop/pods"
        assert request.url.params["labelSelector"] == "app=checkout-api"
        return httpx.Response(
            200,
            json={
                "items": [
                    _pod("checkout-1", ready=False, restarts=7, reason="OOMKilled"),
                    _pod("checkout-2", ready=True, restarts=0, reason=None),
                ]
            },
        )

    tool = PodStatusTool(client=_client(handler))
    result = tool.run({"namespace": "shop", "selector": "app=checkout-api"}, timeout_s=5).data
    _valid(tool, result)
    assert result["oomkilled"] == 1 and result["restarts_total"] == 7 and result["not_ready"] == 1


def test_pod_status_refuses_an_unbound_namespace() -> None:
    tool = PodStatusTool(client=_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ToolError, match="namespace"):
        tool.run({"namespace": "../kube-system", "selector": "app=x"}, timeout_s=5)


# --- changes.recent ----------------------------------------------------------------------------


def test_changes_recent_orders_by_time() -> None:
    def github(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/checkout/deployments"
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "a" * 40,
                    "created_at": "2026-01-01T11:20:00Z",
                    "creator": {"login": "ada"},
                    "environment": "production",
                },
                {
                    "sha": "b" * 40,
                    "created_at": "2025-12-30T09:00:00Z",
                    "creator": {"login": "bo"},
                    "environment": "production",
                },
            ],
        )

    def k8s(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {
                            "name": "checkout-7d9",
                            "creationTimestamp": "2026-01-01T11:50:00Z",
                            "annotations": {"deployment.kubernetes.io/revision": "14"},
                        }
                    }
                ]
            },
        )

    tool = RecentChangesTool(clock=_Clock(), github=_client(github), kubernetes=_client(k8s))
    result = tool.run(
        {
            "repo": "github:acme/checkout",
            "namespace": "shop",
            "selector": "app=checkout",
            "window_min": 180,
        },
        timeout_s=5,
    ).data
    _valid(tool, result)
    changes: Any = result["changes"]
    assert [(c["source"], c["minutes_ago"]) for c in changes] == [
        ("kubernetes", 10),
        ("github", 40),
    ]
    assert changes[1]["actor"] == "ada", "the two-day-old deploy is outside the window"
    assert json.loads(json.dumps(result)) == result


def test_changes_recent_needs_a_source() -> None:
    with pytest.raises(ToolError, match="pass repo"):
        RecentChangesTool(clock=_Clock()).run({"window_min": 60}, timeout_s=5)


# --- registration and config ------------------------------------------------------------------


def test_registry_registers_each_configured_pack_tool() -> None:
    from pathlib import Path  # noqa: PLC0415

    from supportagent.adapters.fakes.events import RecordingEventSink  # noqa: PLC0415
    from supportagent.adapters.fakes.ids import SequentialIdGenerator  # noqa: PLC0415
    from supportagent.bootstrap import build_tool_registry  # noqa: PLC0415
    from supportagent.config.schema import Tools  # noqa: PLC0415
    from tests.unit.test_triage_service import _shadow_config  # noqa: PLC0415

    tools = Tools.model_validate(
        {
            "postgres": {"databases": {"orders": {"dsn": "postgresql://ro@db/orders"}}},
            "prometheus": {"base_url": "http://prometheus:9090"},
            "kubernetes": {"api_url": "https://k8s:6443", "token": "t-fake"},
            "changes": {"github": False},
        }
    )
    base = _shadow_config()
    repo = Path(__file__).resolve().parents[2]
    paths = base.paths.model_copy(
        update={"catalog": str(repo / "catalog" / "services.example.yaml")}
    )
    config = base.model_copy(update={"tools": tools, "paths": paths})
    registry = build_tool_registry(
        config, events=RecordingEventSink(), ids=SequentialIdGenerator(), clock=_Clock()
    )
    assert {
        "postgres.activity",
        "postgres.locks",
        "postgres.slow_queries",
        "postgres.replication",
        "postgres.storage",
        "metrics.query",
        "k8s.pod_status",
        "changes.recent",
    } <= registry.names


def test_postgres_dsn_resolved_from_its_env(tmp_path: Any) -> None:
    from pathlib import Path  # noqa: PLC0415

    from supportagent.config.loader import load_config  # noqa: PLC0415

    repo = Path(__file__).resolve().parents[2]
    raw = (repo / "supportagent.example.yaml").read_text(encoding="utf-8")
    raw += "tools:\n  postgres:\n    databases:\n      orders: {dsn_env: PG_ORDERS_RO}\n"
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
        "PG_ORDERS_RO": "postgresql://ro@db/orders",
    }
    config = load_config(path, env)
    assert config.tools.postgres is not None
    dsn = config.tools.postgres.databases["orders"].dsn.get_secret_value()
    assert dsn == "postgresql://ro@db/orders"
