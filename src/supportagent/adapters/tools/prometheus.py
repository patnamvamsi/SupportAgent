"""``metrics.query`` (WP-M04, ADR-0011): one instant PromQL query, read-only.

The query is a reviewed runbook literal (``literal_args``). Its only
placeholder is ``{{service}}``, filled from ``$service.name`` (a catalog name,
``[a-z0-9-]``), so ticket text never reaches PromQL.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.tools import ToolResult

_MAX_QUERY = 500
_MAX_SERIES = 20
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["query", "series", "count", "max_value"],
    "properties": {
        "query": {"type": "string"},
        "series": {
            "type": "array",
            "maxItems": _MAX_SERIES,
            "items": {
                "type": "object",
                "required": ["labels", "value"],
                "properties": {"labels": {"type": "object"}, "value": {"type": "number"}},
            },
        },
        "count": {"type": "integer"},
        "max_value": {"type": "number"},
    },
}


class PrometheusQueryTool:
    name = "metrics.query"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args = frozenset({"query"})

    def __init__(self, *, client: httpx.Client) -> None:
        self._client = client

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        query = str(args.get("query", ""))
        if not query or len(query) > _MAX_QUERY:
            raise ToolError("metrics.query: a query of 1-500 characters is required")  # noqa: TRY003
        if "{{service}}" in query:
            service = str(args.get("service", ""))
            if not _SERVICE.match(service):
                raise ToolError("metrics.query: {{service}} needs service bound from $service.name")  # noqa: TRY003
            query = query.replace("{{service}}", service)
        body = get_json(self._client, "/api/v1/query", params={"query": query}, timeout_s=timeout_s)
        data = body.get("data", {}) if isinstance(body, dict) else {}
        if not isinstance(body, dict) or body.get("status") != "success":
            raise ToolError(f"metrics.query: Prometheus returned {body!r:.200}")  # noqa: TRY003
        raw = data.get("result", []) if isinstance(data, dict) else []
        series: list[dict[str, object]] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            try:
                number = float(value[1]) if isinstance(value, list) and len(value) == 2 else None  # noqa: PLR2004
            except (TypeError, ValueError):
                number = None
            if number is None or math.isnan(number):
                continue
            series.append({"labels": dict(item.get("metric") or {}), "value": number})
        series.sort(key=lambda s: -float(str(s["value"])))
        truncated = len(series) > _MAX_SERIES
        series = series[:_MAX_SERIES]
        return ToolResult(
            {
                "query": query,
                "series": series,
                "count": len(series),
                "max_value": max((float(str(s["value"])) for s in series), default=0.0),
            },
            truncated,
        )


__all__ = ["SCHEMA", "PrometheusQueryTool"]
