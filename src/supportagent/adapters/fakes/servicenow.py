"""FakeServiceNow (04 §1): an in-memory ``TicketSource`` + ``TicketSink``.

Serves the unit/property/contract/invariant suites. The *sink* side (WP-006)
appends journal entries in order, merges field updates, and lets
``find_marker`` scan the journals (INV-04 reconciliation). The *source* side
(WP-008) is seeded with ``seed_record`` / ``seed_audit`` / ``seed_journal`` and
serves ``poll`` (page the records changed since a cursor) and ``history``
(audit + journal changes for one record, ordered by time).

Test seams:
  * ``queue_fault(exc)`` — the next call raises ``exc`` (FIFO), simulating
    retryable / non-retryable external failures (01 §7.5, 02 §5).
  * ``records`` / ``audit_entries`` / ``journal_history`` — source-side state.
  * ``journal_entries`` / ``fields`` / ``write_count`` — sink-side state.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord
from supportagent.ports.tickets import JournalField, PollBatch, SnTable

_JOURNAL_FIELDS: tuple[JournalField, ...] = (JournalField.WORK_NOTES, JournalField.COMMENTS)


class _Row(TypedDict):
    """One ServiceNow record row (the shape ``seed_record`` stores and ``poll`` reads)."""

    sys_id: str
    number: str | None
    short_description: str | None
    description: str | None
    priority: str | None
    state: str | None
    cmdb_ci: str | None
    caller_id: str | None
    u_alert_source: str | None
    sys_updated_on: datetime
    sys_updated_by: str
    sys_mod_count: int


_FIXED_TS = "2026-01-01 00:00:00"  # timestamps for sink-appended entries (not asserted on)


class FakeServiceNow:
    """In-memory ticketing system (04 §1): implements ``TicketSource`` + ``TicketSink``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # sink side
        self._journals: dict[tuple[str, str, str], list[tuple[str, ExternalRef]]] = {}
        self._fields: dict[tuple[str, str], dict[str, str]] = {}
        self._jseq = 0
        self._writes = 0
        # source side (table -> sys_id -> raw row; the same nested shape the
        # ASGI fake serves, so ``poll``/``records`` read ``_rows[table][sys_id]``)
        self._rows: dict[str, dict[str, _Row]] = {}
        self._audit: dict[tuple[str, str], list[ChangeRecord]] = {}
        self._journal_hist: dict[tuple[str, str], list[ChangeRecord]] = {}
        # faults
        self._faults: deque[ExternalServiceError] = deque()

    # --- test seams ----------------------------------------------------------

    def queue_fault(self, exc: ExternalServiceError) -> None:
        """Queue a fault: the next call raises ``exc`` (FIFO)."""
        self._faults.append(exc)

    def seed_record(  # noqa: PLR0913
        self,
        table: SnTable,
        sys_id: str,
        *,
        number: str | None = None,
        short_description: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        state: str | None = None,
        cmdb_ci: str | None = None,
        caller_id: str | None = None,
        u_alert_source: str | None = None,
        sys_updated_on: datetime,
        sys_updated_by: str = "u1",
        sys_mod_count: int = 1,
    ) -> None:
        """Record a row in the table (source-side state for ``poll``)."""
        with self._lock:
            self._rows.setdefault(table.value, {})[sys_id] = {
                "sys_id": sys_id,
                "number": number,
                "short_description": short_description,
                "description": description,
                "priority": priority,
                "state": state,
                "cmdb_ci": cmdb_ci,
                "caller_id": caller_id,
                "u_alert_source": u_alert_source,
                "sys_updated_on": sys_updated_on,
                "sys_updated_by": sys_updated_by,
                "sys_mod_count": sys_mod_count,
            }

    def seed_audit(  # noqa: PLR0913
        self,
        table: SnTable,
        sys_id: str,
        *,
        user: str,
        ts: datetime,
        fieldname: str | None = None,
        oldvalue: str | None = None,
        newvalue: str | None = None,
        checkpoint: int | None = None,
    ) -> None:
        """Record a ``sys_audit`` field edit (source-side state for ``history``)."""
        with self._lock:
            self._audit.setdefault((table.value, sys_id), []).append(
                ChangeRecord(
                    source="audit",
                    user=user,
                    ts=ts,
                    fieldname=fieldname,
                    oldvalue=oldvalue,
                    newvalue=newvalue,
                    checkpoint=checkpoint,
                )
            )

    def seed_journal(  # noqa: PLR0913
        self,
        table: SnTable,
        sys_id: str,
        *,
        user: str,
        ts: datetime,
        element: str,
        value: str,
    ) -> None:
        """Record a ``sys_journal_field`` entry (source-side state for ``history``)."""
        with self._lock:
            self._journal_hist.setdefault((table.value, sys_id), []).append(
                ChangeRecord(
                    source="journal",
                    user=user,
                    ts=ts,
                    fieldname=element,
                    oldvalue=None,
                    newvalue=value,
                    checkpoint=None,
                    entry_id=f"jh{sum(len(v) for v in self._journal_hist.values()) + 1:05d}",
                )
            )

    def records(self, table: SnTable) -> list[TicketRecord]:
        """A copy of the current rows on ``table`` (source-side assertions)."""
        with self._lock:
            return [self._row_to_record(table, r) for r in self._rows.get(table.value, {}).values()]

    def audit_entries(self, table: SnTable, sys_id: str) -> list[ChangeRecord]:
        """A copy of the audit entries for ``sys_id`` (source-side assertions)."""
        with self._lock:
            return list(self._audit.get((table.value, sys_id), []))

    def journal_history(self, table: SnTable, sys_id: str) -> list[ChangeRecord]:
        """A copy of the journal entries for ``sys_id`` (source-side assertions)."""
        with self._lock:
            return list(self._journal_hist.get((table.value, sys_id), []))

    def journal_entries(self, table: SnTable, sys_id: str, field: JournalField) -> list[str]:
        """A copy of the current entry texts of ``field`` on ``sys_id`` (sink side)."""
        with self._lock:
            entries = self._journals.get((table.value, sys_id, field.value), [])
            return [text for text, _ref in entries]

    def fields(self, table: SnTable, sys_id: str) -> dict[str, str]:
        """A copy of the current field values on ``sys_id`` (sink side)."""
        with self._lock:
            return dict(self._fields.get((table.value, sys_id), {}))

    @property
    def write_count(self) -> int:
        """Number of successful external writes (sends) so far (sink side)."""
        with self._lock:
            return self._writes

    # --- TicketSource (02 §6) ----------------------------------------------

    def poll(self, table: SnTable, since: datetime, limit: int, offset: int = 0) -> PollBatch:
        """Records on ``table`` with ``sys_updated_on >= since``, one page."""
        with self._lock:
            rows = list(self._rows.get(table.value, {}).values())
            selected = [r for r in rows if r["sys_updated_on"] >= since]
            selected.sort(key=lambda r: (r["sys_updated_on"], r["sys_id"]))
            page = selected[offset : offset + limit]
            next_cursor = max((r["sys_updated_on"] for r in page), default=since)
            records = [self._row_to_record(table, r) for r in page]
        return PollBatch(records=records, next_cursor=next_cursor)

    def history(
        self,
        table: SnTable,
        sys_id: str,
        after_mod_count: int,
        since: datetime | None = None,
    ) -> Sequence[ChangeRecord]:
        """Audit (checkpoint > ``after_mod_count``) + journal (``ts >= since``), by ``ts``."""
        with self._lock:
            audit = [
                c
                for c in self._audit.get((table.value, sys_id), [])
                if c.checkpoint is not None and c.checkpoint > after_mod_count
            ]
            journal = [
                c
                for c in self._journal_hist.get((table.value, sys_id), [])
                if since is None or c.ts >= since
            ]
        return sorted(audit + journal, key=lambda c: (c.ts, c.source == "audit"))

    # --- TicketSink (01 §7.5) ----------------------------------------------

    def _maybe_fault(self) -> None:
        if self._faults:
            raise self._faults.popleft()

    def append_journal(
        self,
        table: SnTable,
        sys_id: str,
        field: JournalField,
        text: str,
    ) -> ExternalRef:
        self._maybe_fault()
        with self._lock:
            self._jseq += 1
            ref = ExternalRef(system=table.value, ref=f"snj{self._jseq:04d}")
            self._journals.setdefault((table.value, sys_id, field.value), []).append((text, ref))
            self._writes += 1
        return ref

    def update_fields(
        self,
        table: SnTable,
        sys_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        self._maybe_fault()
        with self._lock:
            # read-compare: a re-apply of equal values is a no-op write (01 §7.5)
            current = self._fields.setdefault((table.value, sys_id), {})
            changed = {k: v for k, v in fields.items() if current.get(k) != v}
            if changed:
                current.update(changed)
                self._writes += 1
        return ExternalRef(system=table.value, ref=sys_id)

    def find_marker(
        self,
        table: SnTable,
        sys_id: str,
        marker: str,
    ) -> ExternalRef | None:
        with self._lock:
            for field in _JOURNAL_FIELDS:
                for text, ref in self._journals.get((table.value, sys_id, field.value), []):
                    lines = [line.strip() for line in text.strip().splitlines()]
                    if lines and lines[-1] == marker:  # 01 §7.5: last line only (BL-003)
                        return ref
        return None

    # --- internals -----------------------------------------------------------

    def _row_to_record(self, table: SnTable, row: _Row) -> TicketRecord:
        return TicketRecord(
            table=table.value,
            sys_id=row["sys_id"],
            number=row.get("number"),
            short_description=row.get("short_description"),
            description=row.get("description"),
            priority=row.get("priority"),
            state=row.get("state"),
            assigned_to=None,
            assignment_group=None,
            cmdb_ci=row.get("cmdb_ci"),
            caller_id=row.get("caller_id"),
            u_alert_source=row.get("u_alert_source"),
            sys_updated_on=row["sys_updated_on"],
            sys_updated_by=row.get("sys_updated_by") or "unknown",
            sys_mod_count=row["sys_mod_count"],
        )


#: BL-030: reference fields a real Table API returns as ``{"link", "value"}``
#: objects unless the request sends ``sysparm_exclude_reference_link=true``.
_REFERENCE_FIELDS = frozenset(
    {
        "assigned_to",
        "assignment_group",
        "caller_id",
        "cmdb_ci",
        "role",
        "sys_security_acl",
        "sys_user_role",
        "user",
    }
)


def _with_reference_links(request: Request, row: Mapping[str, Any]) -> dict[str, Any]:
    """Render ``row`` as a real instance would for this request (BL-030)."""
    if request.query_params.get("sysparm_exclude_reference_link") == "true":
        return dict(row)
    return {
        k: {"link": f"https://fake.service-now.com/api/now/table/x/{v}", "value": v}
        if k in _REFERENCE_FIELDS and isinstance(v, str) and v
        else v
        for k, v in row.items()
    }


class FakeServiceNowHTTP:
    """A starlette ASGI app simulating the ServiceNow Table API (WP-008).

    Backs the real ``ServiceNow`` adapter in the contract suite: it serves the
    same ``/api/now/table/...`` endpoints the adapter calls, so the mapping,
    query construction, and the ``ServiceNowHTTP`` retry policy are all
    exercised. Test seam: ``queue_status`` pushes responses (e.g. a 429 with
    ``Retry-After``, then a 5xx) that are returned in order before the real
    response, letting the suite assert the retry/backoff behaviour.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, dict[str, Any]]] = {}  # table -> sys_id -> row
        self._audit: list[dict[str, Any]] = []
        self._journal: list[dict[str, Any]] = []
        self._faults: deque[tuple[int, dict[str, str], dict[str, Any] | None]] = deque()
        self._patch_count = 0  # number of successful PATCH writes (idempotency seam)
        self._headers: dict[str, str] = {}  # sent on every table-list response
        self._forbidden: set[str] = set()  # tables answering 403 (doctor ACL probes)
        self.token_requests = 0  # OAuth client-credentials token fetches
        self._routes = [
            Route("/oauth_token.do", self._token, methods=["POST"]),
            Route("/api/now/table/sys_audit", self._list_audit, methods=["GET"]),
            Route("/api/now/table/sys_journal_field", self._list_journal, methods=["GET"]),
            Route("/api/now/table/{table}/{sys_id}", self._record, methods=["GET", "PATCH"]),
            Route("/api/now/table/{table}", self._list_table, methods=["GET"]),
        ]
        self.app = Starlette(routes=self._routes)

    # --- test seams ----------------------------------------------------------

    def queue_status(
        self,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> None:
        """Queue a raw HTTP response (FIFO) returned before the real one."""
        self._faults.append((status, dict(headers or {}), body))

    def seed_row(self, table: str, row: Mapping[str, object]) -> None:
        """Any Table API row (``sys_user``, ``sys_security_acl``, …) keyed by its ``sys_id``."""
        with self._lock:
            values = {k: None if v is None else str(v) for k, v in row.items()}
            key = values.get("sys_id") or f"row{len(self._rows.get(table, {}))}"
            self._rows.setdefault(table, {})[key] = values

    def set_response_headers(self, headers: Mapping[str, str]) -> None:
        """Headers on every list response (e.g. ``X-RateLimit-Limit``)."""
        with self._lock:
            self._headers = dict(headers)

    def forbid(self, table: str) -> None:
        """Answer 403 for ``table`` (an integration user without read on it)."""
        with self._lock:
            self._forbidden.add(table)

    def seed_record(  # noqa: PLR0913
        self,
        table: str,
        sys_id: str,
        *,
        number: str | None = None,
        short_description: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        state: str | None = None,
        cmdb_ci: str | None = None,
        caller_id: str | None = None,
        u_alert_source: str | None = None,
        sys_updated_on: datetime | str,
        sys_updated_by: str = "u1",
        sys_mod_count: int = 1,
    ) -> None:
        with self._lock:
            self._rows.setdefault(table, {})[sys_id] = {
                "sys_id": sys_id,
                "number": number,
                "short_description": short_description,
                "description": description,
                "priority": priority,
                "state": state,
                "cmdb_ci": cmdb_ci,
                "caller_id": caller_id,
                "u_alert_source": u_alert_source,
                "sys_updated_on": self._fmt(sys_updated_on),
                "sys_updated_by": sys_updated_by,
                "sys_mod_count": str(sys_mod_count),
            }

    def seed_audit(  # noqa: PLR0913
        self,
        table: str,
        sys_id: str,
        *,
        user: str,
        ts: datetime | str,
        fieldname: str | None = None,
        oldvalue: str | None = None,
        newvalue: str | None = None,
        checkpoint: int | None = None,
    ) -> None:
        with self._lock:
            self._audit.append(
                {
                    "tablename": table,
                    "documentkey": sys_id,
                    "record_checkpoint": str(checkpoint) if checkpoint is not None else None,
                    "fieldname": fieldname,
                    "oldvalue": oldvalue,
                    "newvalue": newvalue,
                    "user": user,
                    "sys_created_on": self._fmt(ts),
                }
            )

    def seed_journal(  # noqa: PLR0913
        self,
        table: str,
        sys_id: str,
        *,
        user: str,
        ts: datetime | str,
        element: str,
        value: str,
    ) -> None:
        with self._lock:
            self._journal.append(
                {
                    "sys_id": f"j{len(self._journal) + 1:05d}",
                    "name": table,
                    "element_id": sys_id,
                    "element": element,
                    "value": value,
                    "sys_created_by": user,
                    "sys_created_on": self._fmt(ts),
                }
            )

    # --- handlers -----------------------------------------------------------

    def _fault_response(self) -> JSONResponse | None:
        if self._faults:
            status, headers, body = self._faults.popleft()
            return JSONResponse(
                content=body if body is not None else {}, status_code=status, headers=headers
            )
        return None

    async def _list_table(self, request: Request) -> JSONResponse:
        fault = self._fault_response()
        if fault is not None:
            return fault
        table = request.path_params["table"]
        query = request.query_params.get("sysparm_query", "")
        limit = int(request.query_params.get("sysparm_limit", "1000"))
        offset = int(request.query_params.get("sysparm_offset", "0"))
        with self._lock:
            if table in self._forbidden:
                return JSONResponse({"error": {"message": "forbidden"}}, status_code=403)
            rows = list(self._rows.get(table, {}).values())
            headers = dict(self._headers)
        rows = self._filter(rows, query)[offset : offset + limit]
        rendered = [_with_reference_links(request, row) for row in rows]
        return JSONResponse({"result": rendered, "count": len(rows)}, headers=headers)

    async def _token(self, request: Request) -> JSONResponse:
        """02 §6 OAuth token: any client-credentials form gets a 30-minute token."""
        form = (await request.body()).decode()
        if "grant_type=client_credentials" not in form:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        with self._lock:
            self.token_requests += 1
        token = {"access_token": "fake-access-token", "token_type": "Bearer"}  # nosec B105 — fake
        return JSONResponse({**token, "expires_in": 1800})

    async def _record(self, request: Request) -> JSONResponse:
        fault = self._fault_response()
        if fault is not None:
            return fault
        table = request.path_params["table"]
        sys_id = request.path_params["sys_id"]
        with self._lock:
            row = self._rows.get(table, {}).get(sys_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if request.method == "GET":
            return JSONResponse({"result": _with_reference_links(request, row)})
        body = await request.json()
        if not isinstance(body, dict) or not all(isinstance(v, str) for v in body.values()):
            # A real instance rejects non-string field values (e.g. a journal array).
            return JSONResponse({"error": "field values must be strings"}, status_code=400)
        with self._lock:
            self._apply_patch(table, sys_id, body)
            self._patch_count += 1
        return JSONResponse({"result": row})

    @property
    def patch_count(self) -> int:
        """Successful PATCH writes so far (idempotency seam for the sink contract)."""
        with self._lock:
            return self._patch_count

    async def _list_audit(self, request: Request) -> JSONResponse:
        fault = self._fault_response()
        if fault is not None:
            return fault
        with self._lock:
            if "sys_audit" in self._forbidden:
                return JSONResponse({"error": {"message": "forbidden"}}, status_code=403)
        query = request.query_params.get("sysparm_query", "")
        limit = int(request.query_params.get("sysparm_limit", "1000"))
        with self._lock:
            rows = list(self._audit)
        rows = self._filter(rows, query)[:limit]
        return JSONResponse({"result": rows, "count": len(rows)})

    async def _list_journal(self, request: Request) -> JSONResponse:
        fault = self._fault_response()
        if fault is not None:
            return fault
        with self._lock:
            if "sys_journal_field" in self._forbidden:
                return JSONResponse({"error": {"message": "forbidden"}}, status_code=403)
        query = request.query_params.get("sysparm_query", "")
        limit = int(request.query_params.get("sysparm_limit", "1000"))
        with self._lock:
            rows = list(self._journal)
        rows = self._filter(rows, query)[:limit]
        return JSONResponse({"result": rows, "count": len(rows)})

    # --- internals -----------------------------------------------------------

    def _fmt(self, value: datetime | str) -> str:
        if isinstance(value, datetime):
            return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        return value

    def _apply_patch(self, table: str, sys_id: str, body: Mapping[str, Any]) -> None:
        """Table API PATCH semantics: setting a journal field (``work_notes`` /
        ``comments``) to a string *appends* one journal entry; any other key
        sets the field (the handler has already rejected non-string values)."""
        row = self._rows.setdefault(table, {})[sys_id]
        journal_fields = {f.value for f in _JOURNAL_FIELDS}
        for key, value in body.items():
            if key in journal_fields:
                self._journal.append(
                    {
                        "sys_id": f"j{len(self._journal) + 1:05d}",
                        "name": table,
                        "element_id": sys_id,
                        "element": key,
                        "value": value,
                        "sys_created_by": "svc",
                        "sys_created_on": _FIXED_TS,
                    }
                )
            else:
                row[key] = value

    @staticmethod
    def _parse_query(query: str) -> tuple[list[tuple[str, str, str]], list[str]]:
        conditions: list[tuple[str, str, str]] = []
        orderby: list[str] = []
        for clause in filter(None, (c.strip() for c in query.split("^"))):
            if clause.startswith("ORDERBY"):
                orderby.append(clause[len("ORDERBY") :].strip())
                continue
            # The leftmost operator wins (``number=INC1`` is ``=``, not ``IN``);
            # at the same position the longer one (``>=`` over ``>``).
            found = [
                (idx, -len(op), op)
                for op in ("IN", ">=", ">", "LIKE", "=")
                if (idx := clause.find(op)) > 0
            ]
            if found:
                idx, _, op = min(found)
                conditions.append((op, clause[:idx].strip(), clause[idx + len(op) :].strip()))
        return conditions, orderby

    @staticmethod
    def _match(actual: Any, op: str, expected: str) -> bool:  # noqa: ANN401
        a = "" if actual is None else str(actual)
        if op in {">=", ">"} and a.lstrip("-").isdigit() and expected.lstrip("-").isdigit():
            # Integer columns (record_checkpoint, sys_mod_count) compare numerically.
            ai, ei = int(a), int(expected)
            return ai >= ei if op == ">=" else ai > ei
        if op == ">=":
            return a >= expected
        if op == ">":
            return a > expected
        if op == "=":
            return a == expected
        if op == "IN":
            return a in expected.split(",")
        return expected in a  # LIKE

    def _filter(self, rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        conditions, orderby = self._parse_query(query)
        for op, field, value in conditions:
            rows = [r for r in rows if self._match(r.get(field), op, value)]
        if orderby:
            rows = sorted(rows, key=lambda r: tuple(str(r.get(f, "")) for f in orderby))
        return rows


class SyncASGITransport(httpx.BaseTransport):
    """A synchronous ``httpx`` transport that drives an ASGI app (test helper).

    ``httpx.ASGITransport`` is async-only, but ``ServiceNowHTTP`` (02 §1) drives
    an injected *synchronous* ``httpx.Client`` in production. This transport
    lets a test point that sync client at ``FakeServiceNowHTTP.app``: each
    ``handle_request`` runs the ASGI app to completion on a throwaway event loop
    and returns the accumulated response. It is intentionally minimal — enough
    to serve the table API, not a general ASGI client (no streaming, websockets,
    or keep-alive).
    """

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self._app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": request.url.scheme,
            "path": request.url.path,
            "query_string": request.url.query,
            "headers": [
                (k.lower().encode("latin-1"), v.encode("latin-1"))
                for k, v in request.headers.items()
            ],
            "client": ("testclient", 50000),
            "server": (request.url.host, request.url.port),
        }
        status = 200
        headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": request.content, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = [(k, v) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._app(scope, receive, send))
        finally:
            loop.close()
        return httpx.Response(
            status, headers=headers, content=b"".join(body_parts), request=request
        )


__all__ = ["FakeServiceNow", "FakeServiceNowHTTP", "SyncASGITransport"]
