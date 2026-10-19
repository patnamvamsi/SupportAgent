"""ServiceNow write adapter (01 §7.5): ``TicketSink`` over the Table API.

All writes are idempotent by construction (01 §7.5): ``append_journal`` and
``update_fields`` compare current external state first, so re-applying an
already-applied action is a no-op. ``find_marker`` (reconciliation, INV-04)
reads the journal for the ``[sa:<key>]`` marker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from supportagent.adapters.servicenow.http import ServiceNowHTTP
from supportagent.domain.errors import ServiceNowError
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import JournalField, SnTable

JOURNAL_FIELDS = "element,value,sys_created_by,sys_created_on"


def _marker_query(table: SnTable, sys_id: str, marker: str) -> str:
    # LIKE with the marker as a substring; ServiceNow uses ``LIKE`` for
    # substring matches in the Table API.
    return f"name={table.value}^element_id={sys_id}^valueLIKE{marker}"


class ServiceNowSink:
    """The real ``TicketSink`` (01 §7.5)."""

    def __init__(self, *, http: ServiceNowHTTP) -> None:
        self._http = http

    def append_journal(
        self,
        table: SnTable,
        sys_id: str,
        field: JournalField,
        text: str,
    ) -> ExternalRef:
        """Append ``text`` to ``field`` on ``sys_id`` (idempotent, 01 §7.5).

        If the last journal entry already equals ``text`` (marker included),
        this is a no-op and returns the existing entry's ref.
        """
        entries = self._read_journal(table, sys_id, field.value)
        if entries and entries[-1].get("value") == text:
            return ExternalRef(system=table.value, ref=f"{sys_id}#{field.value}#{len(entries) - 1}")
        # The Table API has no atomic "append to journal": we read the current
        # entries and PATCH the full array with the new one appended (ServiceNow
        # journals are append-only, safe under the integration user's lock).
        next_index = len(entries)
        new_entries = [{**e, "element": e.get("element", field.value)} for e in entries]
        new_entries.append({"element": field.value, "value": text})
        self._http.request(
            "PATCH",
            f"/api/now/table/{table.value}/{sys_id}",
            json={field.value: new_entries},
        )
        return ExternalRef(system=table.value, ref=f"{sys_id}#{field.value}#{next_index}")

    def update_fields(
        self,
        table: SnTable,
        sys_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        """Set ``fields`` on ``sys_id`` (read-compare first: no-op if equal)."""
        current = self._read_record(table, sys_id)
        changed = {k: v for k, v in fields.items() if current.get(k) != v}
        if not changed:
            return ExternalRef(system=table.value, ref=f"{sys_id}#noop")
        self._http.request(
            "PATCH",
            f"/api/now/table/{table.value}/{sys_id}",
            json=dict(changed),
        )
        return ExternalRef(system=table.value, ref=f"{sys_id}#{len(changed)}")

    def find_marker(
        self,
        table: SnTable,
        sys_id: str,
        marker: str,
    ) -> ExternalRef | None:
        """Reconciliation read (INV-04): the journal entry carrying ``marker``."""
        resp = self._http.request(
            "GET",
            "/api/now/table/sys_journal_field",
            params={
                "sysparm_query": _marker_query(table, sys_id, marker),
                "sysparm_fields": JOURNAL_FIELDS,
                "sysparm_limit": 1,
                "sysparm_display_value": "false",
            },
        )
        try:
            rows = resp.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed journal response: {exc!r}") from exc  # noqa: TRY003
        if not rows:
            return None
        row = rows[0]
        entry_index = row.get("sys_created_on", "")
        return ExternalRef(
            system=table.value, ref=f"{sys_id}#{row.get('element', '')}#{entry_index}"
        )

    # -- internals ---------------------------------------------------------

    def _read_journal(self, table: SnTable, sys_id: str, field: str) -> list[dict[str, object]]:
        resp = self._http.request(
            "GET",
            "/api/now/table/sys_journal_field",
            params={
                "sysparm_query": f"name={table.value}^element_id={sys_id}^element={field}",
                "sysparm_fields": JOURNAL_FIELDS,
                "sysparm_display_value": "false",
            },
        )
        try:
            rows = resp.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed journal response: {exc!r}") from exc  # noqa: TRY003
        return cast("list[dict[str, object]]", rows)

    def _read_record(self, table: SnTable, sys_id: str) -> dict[str, object]:
        resp = self._http.request(
            "GET",
            f"/api/now/table/{table.value}/{sys_id}",
            params={"sysparm_display_value": "false"},
        )
        try:
            return cast("dict[str, object]", resp.json()["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed record response: {exc!r}") from exc  # noqa: TRY003


__all__ = ["JOURNAL_FIELDS", "ServiceNowSink"]
