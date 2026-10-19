"""ServiceNow read adapter (02 §6): ``TicketSource`` over the Table API.

``poll`` pages the changed records since a cursor; ``history`` merges the
``sys_audit`` field edits (checkpointed past ``after_mod_count``) with the
``sys_journal_field`` entries for one record, ordered by time. Both map the
raw JSON into domain types (``mapping``) so no vendor type crosses the port
(01 §3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from supportagent.adapters.servicenow.http import ServiceNowHTTP
from supportagent.adapters.servicenow.mapping import (
    audit_row_to_change,
    journal_row_to_change,
    row_to_record,
)
from supportagent.domain.errors import ServiceNowError
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.ports.tickets import PollBatch, SnTable

POLL_FIELDS = (
    "sys_id,number,short_description,description,priority,state,assigned_to,"
    "assignment_group,cmdb_ci,sys_updated_on,sys_updated_by,sys_mod_count,caller_id"
)
AUDIT_FIELDS = "fieldname,oldvalue,newvalue,user,sys_created_on,record_checkpoint"
JOURNAL_FIELDS = "element,value,sys_created_by,sys_created_on"


def _format_since(since: datetime) -> str:
    """A cursor datetime → the ServiceNow query value (UTC ``YYYY-MM-DD HH:MM:SS``)."""
    return since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


class ServiceNowSource:
    """The real ``TicketSource`` (02 §6)."""

    def __init__(self, *, http: ServiceNowHTTP) -> None:
        self._http = http

    def poll(self, table: SnTable, since: datetime, limit: int, offset: int = 0) -> PollBatch:
        query = f"sys_updated_on>={_format_since(since)}^ORDERBYsys_updated_on^ORDERBYsys_id"
        resp = self._http.request(
            "GET",
            f"/api/now/table/{table.value}",
            params={
                "sysparm_query": query,
                "sysparm_limit": limit,
                "sysparm_offset": offset,
                "sysparm_fields": POLL_FIELDS,
                "sysparm_display_value": "false",
            },
        )
        rows = self._results(resp)
        records = [row_to_record(table.value, row) for row in rows]
        next_cursor = max((r.sys_updated_on for r in records), default=since)
        return PollBatch(records=records, next_cursor=next_cursor)

    def history(
        self,
        table: SnTable,
        sys_id: str,
        after_mod_count: int,
        since: datetime | None = None,
    ) -> Sequence[ChangeRecord]:
        audit = self._audit(table, sys_id, after_mod_count)
        journal = self._journal(table, sys_id, since)
        return sorted(audit + journal, key=lambda c: (c.ts, c.source == "audit"))

    # -- internals ---------------------------------------------------------

    def _audit(self, table: SnTable, sys_id: str, after_mod_count: int) -> list[ChangeRecord]:
        query = (
            f"tablename={table.value}^documentkey={sys_id}"
            f"^record_checkpoint>{after_mod_count}^ORDERBYrecord_checkpoint"
        )
        resp = self._http.request(
            "GET",
            "/api/now/table/sys_audit",
            params={
                "sysparm_query": query,
                "sysparm_fields": AUDIT_FIELDS,
                "sysparm_display_value": "false",
            },
        )
        return [audit_row_to_change(row) for row in self._results(resp)]

    def _journal(self, table: SnTable, sys_id: str, since: datetime | None) -> list[ChangeRecord]:
        # 02 §6: journal entries for the record created at or after ``since``.
        query = f"name={table.value}^element_id={sys_id}"
        if since is not None:
            query += f"^sys_created_on>={_format_since(since)}"
        resp = self._http.request(
            "GET",
            "/api/now/table/sys_journal_field",
            params={
                "sysparm_query": query,
                "sysparm_fields": JOURNAL_FIELDS,
                "sysparm_display_value": "false",
            },
        )
        return [journal_row_to_change(row) for row in self._results(resp)]

    @staticmethod
    def _results(resp: httpx.Response) -> list[dict[str, object]]:
        try:
            payload = resp.json()
            rows = payload["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed Table API response: {exc!r}") from exc  # noqa: TRY003
        if not isinstance(rows, list):
            raise ServiceNowError("Table API response missing 'result' list")  # noqa: TRY003
        return rows


__all__ = ["AUDIT_FIELDS", "JOURNAL_FIELDS", "POLL_FIELDS", "ServiceNowSource"]
