"""``postgres.*`` (WP-M04, ADR-0011): five fixed, read-only diagnostics.

Every query is a constant ``SELECT`` over the ``pg_stat_*`` views written
here; no SQL comes from a runbook, a ticket or the model. Three fences:

1. the database must be one the case's service lists in ``databases:`` (the
   catalog allowlist, bound by ``bind_args``) and one ``tools.postgres``
   configures;
2. the session is read-only (``default_transaction_read_only``) with a
   ``statement_timeout``;
3. :class:`PgReader` refuses anything but a ``SELECT`` / ``WITH``.

The connection should use a role with only ``pg_monitor`` (or ``pg_read_all_stats``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_MAX_ROWS = 50


class PgReader(Protocol):
    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]: ...


class PsycopgReader:
    """A read-only, time-limited session per call (psycopg 3)."""

    def __init__(self, dsns: Mapping[str, str], *, statement_timeout_ms: int = 5000) -> None:
        self._dsns = dict(dsns)
        self._timeout_ms = statement_timeout_ms

    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]:
        import psycopg  # noqa: PLC0415 — only a deployment with tools.postgres needs it
        from psycopg.rows import dict_row  # noqa: PLC0415

        if not _READ_ONLY.match(sql):
            raise ToolError("postgres: only SELECT statements are allowed")  # noqa: TRY003
        dsn = self._dsns.get(database)
        if dsn is None:
            raise ToolError(f"postgres: database {database!r} is not configured")  # noqa: TRY003
        timeout_ms = min(self._timeout_ms, int(timeout_s * 1000))
        options = f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}"
        try:
            with (
                psycopg.connect(
                    dsn, connect_timeout=max(1, int(timeout_s)), options=options, autocommit=True
                ) as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute(sql)  # a constant from this module
                return [_jsonable(row) for row in cur.fetchmany(_MAX_ROWS)]
        except psycopg.OperationalError as exc:
            raise ToolError(f"postgres {database}: {type(exc).__name__}", retryable=True) from exc  # noqa: TRY003
        except psycopg.Error as exc:
            raise ToolError(f"postgres {database}: {type(exc).__name__}: {exc}") from exc  # noqa: TRY003


def _jsonable(row: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime | date):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _num(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    return float(str(value)) if value not in (None, "") else 0.0


def _int(value: object) -> int:
    return int(_num(value))


class _PgTool:
    trust_class = TrustClass.READ
    tier = Tier.T0
    literal_args: frozenset[str] = frozenset()
    name: str
    result_schema: Mapping[str, object]

    def __init__(self, *, reader: PgReader) -> None:
        self._reader = reader

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        raise NotImplementedError

    def _q(self, args: Mapping[str, object], sql: str, timeout_s: float) -> list[dict[str, object]]:
        return self._reader.query(self._db(args), sql, timeout_s=timeout_s)

    def _db(self, args: Mapping[str, object]) -> str:
        database = args.get("database")
        if not isinstance(database, str) or not database:
            raise ToolError(f"{self.name}: a database bound from $service.databases is required")  # noqa: TRY003
        return database


# --- postgres.activity ----------------------------------------------------------------

ACTIVITY_SQL = """
SELECT coalesce(state, 'unknown') AS state,
       count(*) AS n,
       count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting,
       coalesce(max(extract(epoch FROM now() - xact_start)), 0) AS oldest_xact_s
FROM pg_stat_activity
WHERE datname = current_database() AND backend_type = 'client backend'
GROUP BY 1
"""
MAX_CONNECTIONS_SQL = "SELECT current_setting('max_connections')::int AS max_connections"

ACTIVITY_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": [
        "database",
        "total",
        "max_connections",
        "by_state",
        "idle_in_transaction",
        "waiting",
        "oldest_xact_age_s",
    ],
    "properties": {
        "database": {"type": "string"},
        "total": {"type": "integer"},
        "max_connections": {"type": "integer"},
        "by_state": {"type": "object"},
        "idle_in_transaction": {"type": "integer"},
        "waiting": {"type": "integer"},
        "oldest_xact_age_s": {"type": "number"},
    },
}


class PgActivityTool(_PgTool):
    """Connections by state against ``max_connections``: pool exhaustion, leaks, waits."""

    name = "postgres.activity"
    result_schema = ACTIVITY_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        rows = self._q(args, ACTIVITY_SQL, timeout_s)
        limit = self._q(args, MAX_CONNECTIONS_SQL, timeout_s)
        by_state = {str(r["state"]): _int(r["n"]) for r in rows}
        return ToolResult(
            {
                "database": self._db(args),
                "total": sum(by_state.values()),
                "max_connections": _int(limit[0]["max_connections"]) if limit else 0,
                "by_state": by_state,
                "idle_in_transaction": by_state.get("idle in transaction", 0)
                + by_state.get("idle in transaction (aborted)", 0),
                "waiting": sum(_int(r["waiting"]) for r in rows),
                "oldest_xact_age_s": max((_num(r["oldest_xact_s"]) for r in rows), default=0.0),
            }
        )


# --- postgres.locks -------------------------------------------------------------------

LOCKS_SQL = """
SELECT blocked.pid AS blocked_pid,
       blocker.pid AS blocker_pid,
       coalesce(blocker.state, 'unknown') AS blocker_state,
       coalesce(extract(epoch FROM now() - blocker.xact_start), 0) AS blocker_xact_s
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid)
JOIN pg_stat_activity AS blocker ON blocker.pid = b.pid
WHERE blocked.datname = current_database()
"""

LOCKS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "blocked", "blockers"],
    "properties": {
        "database": {"type": "string"},
        "blocked": {"type": "integer"},
        "blockers": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["pid", "blocking", "state", "xact_age_s"],
                "properties": {
                    "pid": {"type": "integer"},
                    "blocking": {"type": "integer"},
                    "state": {"type": "string"},
                    "xact_age_s": {"type": "number"},
                },
            },
        },
    },
}


class PgLocksTool(_PgTool):
    """Who blocks whom: lock contention and the long transaction behind it."""

    name = "postgres.locks"
    result_schema = LOCKS_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        rows = self._q(args, LOCKS_SQL, timeout_s)
        blockers: dict[int, dict[str, object]] = {}
        for row in rows:
            pid = _int(row["blocker_pid"])
            entry = blockers.setdefault(
                pid,
                {
                    "pid": pid,
                    "blocking": 0,
                    "state": str(row["blocker_state"]),
                    "xact_age_s": _num(row["blocker_xact_s"]),
                },
            )
            entry["blocking"] = _int(entry["blocking"]) + 1
        top = sorted(blockers.values(), key=lambda e: -_int(e["blocking"]))[:10]
        return ToolResult(
            {
                "database": self._db(args),
                "blocked": len({_int(r["blocked_pid"]) for r in rows}),
                "blockers": top,
            }
        )


# --- postgres.slow_queries ----------------------------------------------------------------

STATEMENTS_EXT_SQL = "SELECT count(*) AS n FROM pg_extension WHERE extname = 'pg_stat_statements'"
SLOW_SQL = """
SELECT queryid::text AS queryid,
       calls,
       mean_exec_time AS mean_ms,
       total_exec_time AS total_ms,
       left(query, 200) AS query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
ORDER BY mean_exec_time DESC
LIMIT 10
"""

SLOW_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "available", "queries"],
    "properties": {
        "database": {"type": "string"},
        "available": {"type": "boolean"},
        "queries": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["queryid", "calls", "mean_ms", "total_ms"],
                "properties": {
                    "queryid": {"type": "string"},
                    "calls": {"type": "integer"},
                    "mean_ms": {"type": "number"},
                    "total_ms": {"type": "number"},
                    "query": {"type": "string"},
                },
            },
        },
    },
}


class PgSlowQueriesTool(_PgTool):
    """The slowest statements by mean time (``pg_stat_statements``, when installed)."""

    name = "postgres.slow_queries"
    result_schema = SLOW_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        ext = self._q(args, STATEMENTS_EXT_SQL, timeout_s)
        available = bool(ext) and _int(ext[0]["n"]) > 0
        rows = self._q(args, SLOW_SQL, timeout_s) if available else []
        queries = [
            {
                "queryid": str(r["queryid"]),
                "calls": _int(r["calls"]),
                "mean_ms": round(_num(r["mean_ms"]), 3),
                "total_ms": round(_num(r["total_ms"]), 3),
                "query": str(r.get("query") or ""),
            }
            for r in rows
        ]
        return ToolResult({"database": self._db(args), "available": available, "queries": queries})


# --- postgres.replication -----------------------------------------------------------------

RECOVERY_SQL = "SELECT pg_is_in_recovery() AS is_replica"
REPLICAS_SQL = """
SELECT coalesce(application_name, '') AS name,
       coalesce(state, 'unknown') AS state,
       coalesce(extract(epoch FROM replay_lag), 0) AS lag_s
FROM pg_stat_replication
"""
REPLICA_LAG_SQL = (
    "SELECT coalesce(extract(epoch FROM now() - pg_last_xact_replay_timestamp()), 0) AS lag_s"
)

REPLICATION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "is_replica", "replicas", "max_lag_s"],
    "properties": {
        "database": {"type": "string"},
        "is_replica": {"type": "boolean"},
        "replicas": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "state", "lag_s"],
                "properties": {
                    "name": {"type": "string"},
                    "state": {"type": "string"},
                    "lag_s": {"type": "number"},
                },
            },
        },
        "max_lag_s": {"type": "number"},
    },
}


class PgReplicationTool(_PgTool):
    """Replication lag: on a primary, each replica's replay lag; on a replica, its own."""

    name = "postgres.replication"
    result_schema = REPLICATION_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        recovery = self._q(args, RECOVERY_SQL, timeout_s)
        is_replica = bool(recovery and recovery[0]["is_replica"])
        if is_replica:
            lag = self._q(args, REPLICA_LAG_SQL, timeout_s)
            replicas: list[dict[str, object]] = []
            max_lag = _num(lag[0]["lag_s"]) if lag else 0.0
        else:
            replicas = [
                {"name": str(r["name"]), "state": str(r["state"]), "lag_s": _num(r["lag_s"])}
                for r in self._q(args, REPLICAS_SQL, timeout_s)
            ]
            max_lag = max((_num(r["lag_s"]) for r in replicas), default=0.0)
        return ToolResult(
            {
                "database": self._db(args),
                "is_replica": is_replica,
                "replicas": replicas,
                "max_lag_s": max_lag,
            }
        )


# --- postgres.storage ---------------------------------------------------------------------

SIZE_SQL = "SELECT pg_database_size(current_database()) AS size_bytes"
TABLES_SQL = """
SELECT relname AS name,
       n_live_tup AS live,
       n_dead_tup AS dead,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 10
"""

STORAGE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "size_bytes", "tables", "max_dead_ratio"],
    "properties": {
        "database": {"type": "string"},
        "size_bytes": {"type": "integer"},
        "tables": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "required": ["name", "live", "dead", "dead_ratio"],
                "properties": {
                    "name": {"type": "string"},
                    "live": {"type": "integer"},
                    "dead": {"type": "integer"},
                    "dead_ratio": {"type": "number"},
                    "last_autovacuum": {"type": ["string", "null"]},
                },
            },
        },
        "max_dead_ratio": {"type": "number"},
    },
}


class PgStorageTool(_PgTool):
    """Database size and dead-tuple bloat per table (autovacuum falling behind)."""

    name = "postgres.storage"
    result_schema = STORAGE_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        size = self._q(args, SIZE_SQL, timeout_s)
        tables = []
        for r in self._q(args, TABLES_SQL, timeout_s):
            live, dead = _int(r["live"]), _int(r["dead"])
            tables.append(
                {
                    "name": str(r["name"]),
                    "live": live,
                    "dead": dead,
                    "dead_ratio": round(dead / (live + dead), 4) if live + dead else 0.0,
                    "last_autovacuum": None
                    if r.get("last_autovacuum") is None
                    else str(r["last_autovacuum"]),
                }
            )
        return ToolResult(
            {
                "database": self._db(args),
                "size_bytes": _int(size[0]["size_bytes"]) if size else 0,
                "tables": tables,
                "max_dead_ratio": max((_num(t["dead_ratio"]) for t in tables), default=0.0),
            }
        )


POSTGRES_TOOLS: Sequence[type[_PgTool]] = (
    PgActivityTool,
    PgLocksTool,
    PgSlowQueriesTool,
    PgReplicationTool,
    PgStorageTool,
)
ALL_SQL: Sequence[str] = (
    ACTIVITY_SQL,
    MAX_CONNECTIONS_SQL,
    LOCKS_SQL,
    STATEMENTS_EXT_SQL,
    SLOW_SQL,
    RECOVERY_SQL,
    REPLICAS_SQL,
    REPLICA_LAG_SQL,
    SIZE_SQL,
    TABLES_SQL,
)

__all__ = [
    "ALL_SQL",
    "POSTGRES_TOOLS",
    "PgActivityTool",
    "PgLocksTool",
    "PgReader",
    "PgReplicationTool",
    "PgSlowQueriesTool",
    "PgStorageTool",
    "PsycopgReader",
]
