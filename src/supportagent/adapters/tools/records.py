"""``records.query`` (ADR-0021 §4): a reviewed ``SELECT`` over business records, read-only.

The SQL is a reviewed literal from a flow stage or a pack probe
(``literal_args``); the model never writes it. Its only inputs are ``:id``
and ``:key``, sent as bound parameters, so a traced id can't change the
query's shape. ``%`` is literal (``LIKE 'BTC%'``) and ``::type`` casts are
left alone. Fences, as for ``postgres.*``:

1. the database must be one the case's service lists in ``databases:`` (the
   catalog allowlist, bound by ``bind_args``) and one ``tools.records``
   configures: a business-data connection apart from the ``pg_monitor`` one;
2. the session is read-only (``default_transaction_read_only``) with a
   ``statement_timeout``;
3. one statement, starting ``SELECT`` or ``WITH``; at most 50 rows, each value
   a JSON scalar and text capped.

The role should have ``SELECT`` on the tables flows and probes read and
nothing on credentials or sessions. What a stage shows the model is still its
``fields`` allow-list, then PII pseudonymisation (§4a).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_MAX_SQL = 4000
_MAX_ROWS = 50
_MAX_COLUMNS = 40
_MAX_TEXT = 500
#: ``:id`` / ``:key`` as a parameter, not the tail of a ``::cast``.
_PARAM = re.compile(r"(?<![:\w]):(id|key)\b")
_PARAMS = ("id", "key")

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["database", "count", "rows"],
    "properties": {
        "database": {"type": "string"},
        "count": {"type": "integer"},
        "rows": {"type": "array", "maxItems": _MAX_ROWS, "items": {"type": "object"}},
    },
}


class RecordReader(Protocol):
    def records(
        self,
        database: str,
        sql: str,
        params: Mapping[str, str],
        *,
        limit: int,
        timeout_s: float,
    ) -> tuple[list[dict[str, object]], bool]:
        """Up to ``limit`` rows, and whether there were more."""
        ...


def prepare(sql: str, args: Mapping[str, object]) -> tuple[str, dict[str, str]]:
    """The statement in psycopg's format, and the parameters it uses (any backend)."""
    if not sql or len(sql) > _MAX_SQL:
        raise ToolError(f"records.query: a statement of 1-{_MAX_SQL} characters is required")  # noqa: TRY003
    if not _READ_ONLY.match(sql):
        raise ToolError("records.query: only SELECT statements are allowed")  # noqa: TRY003
    if ";" in sql.rstrip().removesuffix(";"):
        raise ToolError("records.query: one statement only")  # noqa: TRY003
    used = set(_PARAM.findall(sql))
    params: dict[str, str] = {}
    for name in sorted(used):
        value = args.get(name)
        if not isinstance(value, str) or not value:
            raise ToolError(f"records.query: the statement uses :{name}, which isn't given")  # noqa: TRY003
        params[name] = value
    extra = [n for n in _PARAMS if n in args and n not in used]
    if extra:
        raise ToolError(f"records.query: {', '.join(extra)} given but not used")  # noqa: TRY003
    body = sql.rstrip().removesuffix(";").replace("%", "%%")
    return _PARAM.sub(lambda m: f"%({m.group(1)})s", body), params


class PsycopgRecordReader:
    """A read-only, time-limited session per call (psycopg 3)."""

    def __init__(self, dsns: Mapping[str, str], *, statement_timeout_ms: int = 5000) -> None:
        self._dsns = dict(dsns)
        self._timeout_ms = statement_timeout_ms

    def records(
        self,
        database: str,
        sql: str,
        params: Mapping[str, str],
        *,
        limit: int,
        timeout_s: float,
    ) -> tuple[list[dict[str, object]], bool]:
        import psycopg  # noqa: PLC0415 — only a deployment with tools.records needs it
        from psycopg.rows import dict_row  # noqa: PLC0415

        dsn = self._dsns.get(database)
        if dsn is None:
            raise ToolError(f"records.query: database {database!r} is not configured")  # noqa: TRY003
        timeout_ms = min(self._timeout_ms, int(timeout_s * 1000))
        options = f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}"
        try:
            with (
                psycopg.connect(
                    dsn, connect_timeout=max(1, int(timeout_s)), options=options, autocommit=True
                ) as conn,
                conn.cursor(row_factory=dict_row) as cur,
            ):
                cur.execute(sql, dict(params))  # a reviewed literal; values are bound
                rows = cur.fetchmany(limit + 1)
        except psycopg.OperationalError as exc:
            raise ToolError(  # noqa: TRY003
                f"records.query {database}: {type(exc).__name__}", retryable=True
            ) from exc
        except psycopg.Error as exc:
            raise ToolError(f"records.query {database}: {type(exc).__name__}: {exc}") from exc  # noqa: TRY003
        return [dict(r) for r in rows[:limit]], len(rows) > limit


def _scalar(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    return str(value)[:_MAX_TEXT]


def _row(row: Mapping[str, object]) -> dict[str, object]:
    return {str(k): _scalar(v) for k, v in list(row.items())[:_MAX_COLUMNS]}


class RecordsQueryTool:
    name = "records.query"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args = frozenset({"sql", "limit", *_PARAMS})

    def __init__(self, *, reader: RecordReader) -> None:
        self._reader = reader

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        database = args.get("database")
        if not isinstance(database, str) or not database:
            raise ToolError("records.query: a database bound from $service.databases is required")  # noqa: TRY003
        sql, params = prepare(str(args.get("sql", "")), args)
        try:
            limit = int(str(args.get("limit", _MAX_ROWS)))
        except ValueError:
            raise ToolError("records.query: limit must be an integer") from None  # noqa: TRY003
        if not 1 <= limit <= _MAX_ROWS:
            raise ToolError(f"records.query: limit must be 1..{_MAX_ROWS}")  # noqa: TRY003
        rows, more = self._reader.records(database, sql, params, limit=limit, timeout_s=timeout_s)
        out = [_row(r) for r in rows]
        return ToolResult({"database": database, "count": len(out), "rows": out}, more)


__all__ = ["SCHEMA", "PsycopgRecordReader", "RecordReader", "RecordsQueryTool", "prepare"]
