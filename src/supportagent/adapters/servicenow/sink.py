"""ServiceNow write adapter (01 §7.5): ``TicketSink`` over the Table API.

``update_fields`` is idempotent by read-compare (01 §7.5). ``append_journal``
appends one entry per call (journals are append-only); its duplicate guard is
``find_marker`` (reconciliation, INV-04), which the executor runs before every
journal send.
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
        """Append ``text`` as one ``field`` entry on ``sys_id`` (02 §6).

        On the Table API, PATCHing a journal field with a string *appends* an
        entry; journals cannot be rewritten. There is no pre-read: duplicate
        protection is the executor's ``find_marker`` reconciliation (INV-04),
        and a deliberate identical note must still post.
        """
        self._http.request(
            "PATCH",
            f"/api/now/table/{table.value}/{sys_id}",
            json={field.value: text},
        )
        return ExternalRef(system=table.value, ref=f"{sys_id}#{field.value}")

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
