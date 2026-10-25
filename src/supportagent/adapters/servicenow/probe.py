"""``ServiceNowProbe`` over the Table API (02 §4.1 doctor checks).

GET only: the class has no method that writes, so ``doctor`` can't write
to ServiceNow whatever a check does. Values come back as strings
(``sysparm_display_value=false``; ``None`` → ``""``), and header names are
lower-cased so checks can look up ``x-ratelimit-limit`` without caring
about case.
"""

from __future__ import annotations

from collections.abc import Sequence

from supportagent.adapters.servicenow.http import ServiceNowHTTP
from supportagent.domain.errors import ServiceNowError
from supportagent.ports.diagnostics import ProbeRows


def _flat(value: object) -> str:
    """A field value as text; a ``{"link", "value"}`` reference yields its ``value`` (BL-030)."""
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else str(value)


class ServiceNowTableProbe:
    """Read-only Table API access for the doctor."""

    def __init__(self, *, http: ServiceNowHTTP) -> None:
        self._http = http

    def read(self, table: str, *, query: str, fields: Sequence[str], limit: int) -> ProbeRows:
        params: dict[str, object] = {
            "sysparm_fields": ",".join(fields),
            "sysparm_limit": limit,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        }
        if query:
            params["sysparm_query"] = query
        resp = self._http.request("GET", f"/api/now/table/{table}", params=params)
        try:
            rows = resp.json()["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceNowError(f"malformed Table API response: {exc!r}") from exc  # noqa: TRY003
        if not isinstance(rows, list):
            raise ServiceNowError("Table API response missing 'result' list")  # noqa: TRY003
        return ProbeRows(
            rows=[
                {str(k): _flat(v) for k, v in row.items()} for row in rows if isinstance(row, dict)
            ],
            headers={k.lower(): v for k, v in resp.headers.items()},
        )


__all__ = ["ServiceNowTableProbe"]
