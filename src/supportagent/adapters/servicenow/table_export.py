"""Read-only ServiceNow export for onboarding (ADR-0015 P1, WP-O2).

Pages through the Table API with ``sysparm_offset`` and returns flat
records: every value a string, reference fields dot-walked
(``cmdb_ci.name``) so a record reads the way a person sees it. GET only,
through the same ``ServiceNowHTTP`` client (retries, rate limits) as sync.
"""

from __future__ import annotations

from collections.abc import Sequence

from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.adapters.servicenow.http import ServiceNowHTTP


def export_table(  # noqa: PLR0913 — the Table API's query parameters
    http: ServiceNowHTTP,
    table: str,
    *,
    query: str,
    fields: Sequence[str],
    limit: int,
    page_size: int = 200,
) -> tuple[list[dict[str, str]], bool]:
    """Up to ``limit`` records of ``table`` matching ``query``; and whether more were left."""
    out: list[dict[str, str]] = []
    offset = 0
    while len(out) < limit:
        size = min(page_size, limit - len(out))
        params: dict[str, object] = {
            "sysparm_query": query,
            "sysparm_fields": ",".join(fields),
            "sysparm_limit": size,
            "sysparm_offset": offset,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        resp = http.request("GET", f"/api/now/table/{table}", params=params)
        try:
            rows = resp.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed Table API response for {table}: {exc!r}") from exc  # noqa: TRY003
        if not isinstance(rows, list):
            raise ServiceNowError(f"Table API response for {table} has no result list")  # noqa: TRY003
        out.extend({k: _flat(row.get(k)) for k in fields} for row in rows if isinstance(row, dict))
        if len(rows) < size:
            return out, False
        offset += len(rows)
    return out, True


def _flat(value: object) -> str:
    if isinstance(value, dict):  # a reference without exclude_reference_link
        return str(value.get("display_value") or value.get("value") or "")
    return "" if value is None else str(value)


__all__ = ["export_table"]
