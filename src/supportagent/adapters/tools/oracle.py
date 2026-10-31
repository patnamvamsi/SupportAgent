"""``oracle.*`` (WP-O6a, ADR-0015 §2): four fixed, read-only diagnostics on Oracle.

The oracle plugin's first-party backend. Every statement is a constant
``SELECT`` over the views its ``oracle_ro`` contract grants (``V$SESSION``,
``V$SQL``, ``DBA_TABLESPACE_USAGE_METRICS``); no SQL comes from a runbook, a
ticket or the model. Three fences, as for ``postgres.*``:

1. the database must be one the case's service lists in ``databases:`` (the
   catalog allowlist, bound by ``bind_args``) and one ``tools.oracle`` configures;
2. each call runs in a ``SET TRANSACTION READ ONLY`` transaction with a call
   timeout, and is rolled back;
3. :class:`OracleReader` refuses anything but a ``SELECT`` / ``WITH``.

The driver (``python-oracledb``, thin mode) is not a core dependency: a
deployment that configures ``tools.oracle`` installs it. Nothing here needs
the Diagnostics Pack (ASH, AWR): plan changes are read from the cursors
still in ``V$SQL``.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_MAX_ROWS = 200
_TOP = 10


class OracleReader(Protocol):
    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]: ...


class OracledbReader:
    """A read-only, time-limited transaction per call (python-oracledb, thin mode)."""

    def __init__(self, dsns: Mapping[str, str], *, call_timeout_ms: int = 5000) -> None:
        self._dsns = dict(dsns)
        self._timeout_ms = call_timeout_ms

    def query(self, database: str, sql: str, *, timeout_s: float) -> list[dict[str, object]]:
        if not _READ_ONLY.match(sql):
            raise ToolError("oracle: only SELECT statements are allowed")  # noqa: TRY003
        dsn = self._dsns.get(database)
        if dsn is None:
            raise ToolError(f"oracle: database {database!r} is not configured")  # noqa: TRY003
        try:
            oracledb = importlib.import_module("oracledb")
        except ImportError as exc:
            raise ToolError("oracle: python-oracledb is not installed") from exc  # noqa: TRY003
        try:
            conn = oracledb.connect(dsn=dsn)
        except oracledb.Error as exc:
            raise ToolError(f"oracle {database}: {type(exc).__name__}", retryable=True) from exc  # noqa: TRY003
        try:
            conn.call_timeout = min(self._timeout_ms, int(timeout_s * 1000))
            cur = conn.cursor()
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql)  # a constant from this module
            names = [str(d[0]).lower() for d in cur.description or ()]
            fetched = cur.fetchmany(_MAX_ROWS)
            rows = [_jsonable(dict(zip(names, row, strict=True))) for row in fetched]
            conn.rollback()
        except oracledb.Error as exc:
            raise ToolError(f"oracle {database}: {type(exc).__name__}: {exc}") from exc  # noqa: TRY003
        finally:
            conn.close()
        return rows


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


class _OracleTool:
    trust_class = TrustClass.READ
    tier = Tier.T0
    literal_args: frozenset[str] = frozenset()
    name: str
    result_schema: Mapping[str, object]

    def __init__(self, *, reader: OracleReader) -> None:
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


# --- oracle.sessions -------------------------------------------------------------------

SESSIONS_SQL = """
SELECT count(*) AS total,
       sum(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END) AS active,
       sum(CASE WHEN blocking_session IS NOT NULL THEN 1 ELSE 0 END) AS blocked
FROM v$session
WHERE type = 'USER'
"""
WAITS_SQL = """
SELECT event, count(*) AS sessions
FROM v$session
WHERE type = 'USER' AND state = 'WAITING' AND wait_class <> 'Idle'
GROUP BY event
ORDER BY count(*) DESC
FETCH FIRST 5 ROWS ONLY
"""

SESSIONS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "total", "active", "blocked", "top_waits"],
    "properties": {
        "database": {"type": "string"},
        "total": {"type": "integer"},
        "active": {"type": "integer"},
        "blocked": {"type": "integer"},
        "top_waits": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "required": ["event", "sessions"],
                "properties": {"event": {"type": "string"}, "sessions": {"type": "integer"}},
            },
        },
    },
}


class OracleSessionsTool(_OracleTool):
    """User sessions: how many, how many active or blocked, and what they wait on."""

    name = "oracle.sessions"
    result_schema = SESSIONS_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        counts = (self._q(args, SESSIONS_SQL, timeout_s) or [{}])[0]
        waits = self._q(args, WAITS_SQL, timeout_s)
        return ToolResult(
            {
                "database": self._db(args),
                "total": _int(counts.get("total")),
                "active": _int(counts.get("active")),
                "blocked": _int(counts.get("blocked")),
                "top_waits": [
                    {"event": str(w["event"]), "sessions": _int(w["sessions"])} for w in waits[:5]
                ],
            }
        )


# --- oracle.locks ----------------------------------------------------------------------

LOCKS_SQL = """
SELECT w.sid AS waiter_sid,
       w.blocking_session AS blocker_sid,
       w.event AS event,
       round(w.wait_time_micro / 1000000) AS wait_s,
       b.status AS blocker_status,
       b.sql_id AS blocker_sql_id
FROM v$session w
JOIN v$session b ON b.sid = w.blocking_session
WHERE w.blocking_session IS NOT NULL
"""

LOCKS_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "blocked", "blockers"],
    "properties": {
        "database": {"type": "string"},
        "blocked": {"type": "integer"},
        "blockers": {
            "type": "array",
            "maxItems": _TOP,
            "items": {
                "type": "object",
                "required": ["sid", "blocking", "status", "event", "wait_s"],
                "properties": {
                    "sid": {"type": "integer"},
                    "blocking": {"type": "integer"},
                    "status": {"type": "string"},
                    "sql_id": {"type": ["string", "null"]},
                    #: What its waiters wait on (``enq: TX - row lock contention``).
                    "event": {"type": "string"},
                    #: The longest any of its waiters has waited.
                    "wait_s": {"type": "number"},
                },
            },
        },
    },
}


class OracleLocksTool(_OracleTool):
    """Blocking chains: who blocks whom, on what, and for how long."""

    name = "oracle.locks"
    result_schema = LOCKS_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        rows = self._q(args, LOCKS_SQL, timeout_s)
        blockers: dict[int, dict[str, object]] = {}
        for row in rows:
            sid = _int(row["blocker_sid"])
            entry = blockers.setdefault(
                sid,
                {
                    "sid": sid,
                    "blocking": 0,
                    "status": str(row.get("blocker_status") or "UNKNOWN"),
                    "sql_id": row.get("blocker_sql_id") or None,
                    "event": str(row.get("event") or ""),
                    "wait_s": 0.0,
                },
            )
            entry["blocking"] = _int(entry["blocking"]) + 1
            entry["wait_s"] = max(_num(entry["wait_s"]), _num(row.get("wait_s")))
        top = sorted(blockers.values(), key=lambda e: -_int(e["blocking"]))[:_TOP]
        return ToolResult(
            {
                "database": self._db(args),
                "blocked": len({_int(r["waiter_sid"]) for r in rows}),
                "blockers": top,
            }
        )


# --- oracle.top_sql --------------------------------------------------------------------

TOP_SQL_SQL = """
SELECT sql_id,
       plan_hash_value,
       sum(executions) AS executions,
       sum(elapsed_time) AS elapsed_us,
       max(last_active_time) AS last_active
FROM v$sql
WHERE executions > 0 AND parsing_schema_name NOT IN ('SYS', 'SYSTEM')
GROUP BY sql_id, plan_hash_value
ORDER BY sum(elapsed_time) DESC
FETCH FIRST 200 ROWS ONLY
"""

TOP_SQL_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "statements", "max_slowdown"],
    "properties": {
        "database": {"type": "string"},
        "statements": {
            "type": "array",
            "maxItems": _TOP,
            "items": {
                "type": "object",
                "required": ["sql_id", "plans", "mean_ms", "plan_changed", "slowdown"],
                "properties": {
                    "sql_id": {"type": "string"},
                    "plans": {"type": "integer"},
                    #: Mean elapsed per execution on the most recently used plan.
                    "mean_ms": {"type": "number"},
                    "previous_mean_ms": {"type": ["number", "null"]},
                    "plan_changed": {"type": "boolean"},
                    #: ``mean_ms`` over the best other plan's; 1.0 with one plan.
                    "slowdown": {"type": "number"},
                },
            },
        },
        "max_slowdown": {"type": "number"},
    },
}


class OracleTopSqlTool(_OracleTool):
    """The heaviest statements, and whether one got slower on a new execution plan."""

    name = "oracle.top_sql"
    result_schema = TOP_SQL_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        plans: dict[str, list[dict[str, object]]] = {}
        for row in self._q(args, TOP_SQL_SQL, timeout_s):
            execs = max(1, _int(row["executions"]))
            plans.setdefault(str(row["sql_id"]), []).append(
                {
                    "mean_ms": _num(row["elapsed_us"]) / execs / 1000,
                    "elapsed_us": _num(row["elapsed_us"]),
                    "last_active": str(row.get("last_active") or ""),
                }
            )
        statements = []
        for sql_id, used in plans.items():
            used.sort(key=lambda p: str(p["last_active"]))
            current = _num(used[-1]["mean_ms"])
            others = [_num(p["mean_ms"]) for p in used[:-1]]
            previous = min(others) if others else None
            slowdown = current / previous if previous else 1.0
            statements.append(
                {
                    "sql_id": sql_id,
                    "plans": len(used),
                    "mean_ms": round(current, 3),
                    "previous_mean_ms": round(previous, 3) if previous is not None else None,
                    "plan_changed": len(used) > 1,
                    "slowdown": round(slowdown, 3),
                    "_elapsed": sum(_num(p["elapsed_us"]) for p in used),
                }
            )
        # A regressed statement ranks first, then the heaviest.
        statements.sort(key=lambda s: (-_num(s["slowdown"]), -_num(s["_elapsed"])))
        top = [{k: v for k, v in s.items() if k != "_elapsed"} for s in statements[:_TOP]]
        return ToolResult(
            {
                "database": self._db(args),
                "statements": top,
                "max_slowdown": max((_num(s["slowdown"]) for s in top), default=1.0),
            }
        )


# --- oracle.tablespaces ----------------------------------------------------------------

TABLESPACES_SQL = """
SELECT tablespace_name, used_percent
FROM dba_tablespace_usage_metrics
ORDER BY used_percent DESC
FETCH FIRST 20 ROWS ONLY
"""

TABLESPACES_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "tablespaces", "max_used_pct"],
    "properties": {
        "database": {"type": "string"},
        "tablespaces": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "required": ["name", "used_pct"],
                "properties": {"name": {"type": "string"}, "used_pct": {"type": "number"}},
            },
        },
        #: Of the maximum size, autoextend included (Oracle computes it so).
        "max_used_pct": {"type": "number"},
    },
}


class OracleTablespacesTool(_OracleTool):
    """Tablespace usage against each one's maximum size."""

    name = "oracle.tablespaces"
    result_schema = TABLESPACES_SCHEMA

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        rows = self._q(args, TABLESPACES_SQL, timeout_s)
        spaces = [
            {"name": str(r["tablespace_name"]), "used_pct": round(_num(r["used_percent"]), 2)}
            for r in rows[:20]
        ]
        return ToolResult(
            {
                "database": self._db(args),
                "tablespaces": spaces,
                "max_used_pct": max((_num(s["used_pct"]) for s in spaces), default=0.0),
            }
        )


ORACLE_TOOLS: Sequence[type[_OracleTool]] = (
    OracleSessionsTool,
    OracleLocksTool,
    OracleTopSqlTool,
    OracleTablespacesTool,
)
ALL_SQL: Sequence[str] = (SESSIONS_SQL, WAITS_SQL, LOCKS_SQL, TOP_SQL_SQL, TABLESPACES_SQL)

__all__ = [
    "ALL_SQL",
    "ORACLE_TOOLS",
    "OracleLocksTool",
    "OracleReader",
    "OracleSessionsTool",
    "OracleTablespacesTool",
    "OracleTopSqlTool",
    "OracledbReader",
]
