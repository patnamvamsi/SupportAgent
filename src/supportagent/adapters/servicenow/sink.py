"""ServiceNow write adapter (01 §7.5): ``TicketSink`` over the Table API.

``update_fields`` is idempotent by read-compare (01 §7.5). ``append_journal``
appends one entry per call (journals are append-only); its duplicate guard is
``find_marker`` (reconciliation, INV-04), which the executor runs before every
journal send.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.adapters.servicenow.http import ServiceNowHTTP
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import NoteVisibility, RecordType

JOURNAL_FIELDS = "element,value,sys_created_by,sys_created_on"
#: WP-M07: the journal field each note visibility is written to.
JOURNAL_ELEMENT: dict[NoteVisibility, str] = {
    NoteVisibility.INTERNAL: "work_notes",
    NoteVisibility.CUSTOMER: "comments",
}


#: LIKE candidates fetched per reconciliation read; the last-line check filters them.
_MARKER_CANDIDATES = 10


def marker_on_last_line(text: str, marker: str) -> bool:
    """True when ``marker`` is the entry's last non-empty line (01 §7.5)."""
    lines = [line.strip() for line in text.strip().splitlines()]
    return bool(lines) and lines[-1] == marker


def _marker_query(table: RecordType, external_id: str, marker: str) -> str:
    # LIKE with the marker as a substring; ServiceNow uses ``LIKE`` for
    # substring matches in the Table API.
    return f"name={table.value}^element_id={external_id}^valueLIKE{marker}"


class ServiceNowSink:
    """The real ``TicketSink`` (01 §7.5)."""

    def __init__(self, *, http: ServiceNowHTTP) -> None:
        self._http = http

    def append_journal(
        self,
        table: RecordType,
        external_id: str,
        visibility: NoteVisibility,
        text: str,
    ) -> ExternalRef:
        """Append ``text`` as one journal entry on ``external_id`` (02 §6).

        ``internal`` notes go to ``work_notes``, ``customer`` ones to ``comments``.

        On the Table API, PATCHing a journal field with a string *appends* an
        entry; journals cannot be rewritten. There is no pre-read: duplicate
        protection is the executor's ``find_marker`` reconciliation (INV-04),
        and a deliberate identical note must still post.
        """
        element = JOURNAL_ELEMENT[visibility]
        self._http.request(
            "PATCH",
            f"/api/now/table/{table.value}/{external_id}",
            json={element: text},
        )
        return ExternalRef(system=table.value, ref=f"{external_id}#{element}")

    def update_fields(
        self,
        table: RecordType,
        external_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        """Set ``fields`` on ``sys_id`` (read-compare first: no-op if equal)."""
        current = self._read_record(table, external_id)
        changed = {k: v for k, v in fields.items() if current.get(k) != v}
        if not changed:
            return ExternalRef(system=table.value, ref=f"{external_id}#noop")
        self._http.request(
            "PATCH",
            f"/api/now/table/{table.value}/{external_id}",
            json=dict(changed),
        )
        return ExternalRef(system=table.value, ref=f"{external_id}#{len(changed)}")

    def find_marker(
        self,
        table: RecordType,
        external_id: str,
        marker: str,
    ) -> ExternalRef | None:
        """Reconciliation read (INV-04): the journal entry carrying ``marker``."""
        resp = self._http.request(
            "GET",
            "/api/now/table/sys_journal_field",
            params={
                "sysparm_query": _marker_query(table, external_id, marker),
                "sysparm_fields": JOURNAL_FIELDS,
                "sysparm_limit": _MARKER_CANDIDATES,
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
        )
        try:
            rows = resp.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed journal response: {exc!r}") from exc  # noqa: TRY003
        # 01 §7.5: the marker counts only as the entry's last line, so a person
        # quoting a marker inside their own note never reconciles a send (BL-003).
        row = next((r for r in rows if marker_on_last_line(str(r.get("value", "")), marker)), None)
        if row is None:
            return None
        entry_index = row.get("sys_created_on", "")
        return ExternalRef(
            system=table.value, ref=f"{external_id}#{row.get('element', '')}#{entry_index}"
        )

    # -- internals ---------------------------------------------------------

    def _read_record(self, table: RecordType, external_id: str) -> dict[str, object]:
        resp = self._http.request(
            "GET",
            f"/api/now/table/{table.value}/{external_id}",
            params={"sysparm_display_value": "false", "sysparm_exclude_reference_link": "true"},
        )
        try:
            return cast("dict[str, object]", resp.json()["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed record response: {exc!r}") from exc  # noqa: TRY003


__all__ = ["JOURNAL_ELEMENT", "JOURNAL_FIELDS", "ServiceNowSink"]
