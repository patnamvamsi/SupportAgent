"""``logs.search`` (BL-050, ADR-0011 §2): recent log lines from Loki, read-only.

The LogQL is a reviewed runbook literal (``literal_args``). Its only
placeholder is ``{{service}}``, filled from ``$service.name`` (a catalog name,
``[a-z0-9-]``), so ticket text never reaches LogQL. A query must start with a
stream selector that has a positive matcher, so a check can't scan every
stream. The window and line count are capped. Lines are evidence: checks read
them as fields, and they reach a model only as redacted, fenced ``entail``
evidence (INV-06).

The first-party adapter calls Loki's ``query_range``; the MCP binding calls
``mcp-grafana``'s ``query_loki_logs`` (profile ``grafana.query_loki_logs``).
Both return :func:`streams_result`, so pack checks see one shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.ports.clock import Clock
from supportagent.ports.tools import ToolResult

_MAX_QUERY = 500
_MAX_LINES = 100
_MAX_LINE_CHARS = 500
_DEFAULT_LIMIT = 50
_DEFAULT_WINDOW_MIN = 60
_MAX_WINDOW_MIN = 24 * 60
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
#: ``{label="value"`` or ``{label=~"value"``: one positive matcher with a value
#: that selects something (not ``.*`` / ``.+``, which would scan every stream).
_SELECTOR = re.compile(r'^\{\s*[A-Za-z_][A-Za-z0-9_]*\s*(=|=~)\s*"([^"]+)"')
_MATCH_ALL = frozenset({".*", ".+"})
_NS = 1_000_000_000
#: Loki 3 keeps stream labels apart from structured metadata (``detected_level``)
#: with this flag, which is what ``mcp-grafana`` reports as ``labels`` too.
_CATEGORIZE = {"X-Loki-Response-Encoding-Flags": "categorize-labels"}

SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["query", "window_min", "count", "matches"],
    "properties": {
        "query": {"type": "string"},
        "window_min": {"type": "integer"},
        "count": {"type": "integer"},
        "matches": {
            "type": "array",
            "maxItems": _MAX_LINES,
            "items": {
                "type": "object",
                "required": ["ts", "line", "labels"],
                "properties": {
                    "ts": {"type": "string"},
                    "line": {"type": "string"},
                    "labels": {"type": "object"},
                },
            },
        },
    },
}


def render_logql(args: Mapping[str, object]) -> tuple[str, int, int]:
    """The reviewed LogQL with ``{{service}}`` filled, plus the window and limit (any backend)."""
    query = str(args.get("query", ""))
    if not query or len(query) > _MAX_QUERY:
        raise ToolError("logs.search: a query of 1-500 characters is required")  # noqa: TRY003
    if "{{service}}" in query:
        service = str(args.get("service", ""))
        if not _SERVICE.match(service):
            raise ToolError("logs.search: {{service}} needs service bound from $service.name")  # noqa: TRY003
        query = query.replace("{{service}}", service)
    selector = _SELECTOR.match(query)
    if selector is None or (selector.group(1) == "=~" and selector.group(2) in _MATCH_ALL):
        raise ToolError(  # noqa: TRY003
            'logs.search: start with a stream selector with a positive matcher, e.g. {service="x"}'
        )
    window = _bounded(args.get("window_min", _DEFAULT_WINDOW_MIN), 1, _MAX_WINDOW_MIN, "window_min")
    limit = _bounded(args.get("limit", _DEFAULT_LIMIT), 1, _MAX_LINES, "limit")
    return query, window, limit


def _bounded(value: object, low: int, high: int, name: str) -> int:
    try:
        number = int(str(value))
    except ValueError:
        raise ToolError(f"logs.search: {name} must be an integer") from None  # noqa: TRY003
    if not low <= number <= high:
        raise ToolError(f"logs.search: {name} must be {low}..{high}")  # noqa: TRY003
    return number


def _rfc3339(ns: object) -> str:
    try:
        seconds = int(str(ns)) / _NS
    except ValueError:
        return str(ns)
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def streams_result(
    query: str, window_min: int, entries: Iterable[tuple[object, str, Mapping[str, object]]]
) -> ToolResult:
    """``(ns timestamp, line, labels)`` entries as the capability result, newest first, capped."""
    rows = sorted(entries, key=lambda e: int(str(e[0])) if str(e[0]).isdigit() else 0, reverse=True)
    matches = [
        {
            "ts": _rfc3339(ts),
            "line": line[:_MAX_LINE_CHARS],
            "labels": {str(k): str(v) for k, v in labels.items()},
        }
        for ts, line, labels in rows[:_MAX_LINES]
    ]
    return ToolResult(
        {"query": query, "window_min": window_min, "count": len(matches), "matches": matches},
        len(rows) > _MAX_LINES,
    )


class LokiSearchTool:
    name = "logs.search"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = SCHEMA
    literal_args = frozenset({"query", "window_min", "limit"})

    def __init__(self, *, client: httpx.Client, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        query, window, limit = render_logql(args)
        end = self._clock.now()
        start = end - timedelta(minutes=window)
        body = get_json(
            self._client,
            "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": int(start.timestamp() * _NS),
                "end": int(end.timestamp() * _NS),
                "limit": limit,
                "direction": "backward",
            },
            headers=_CATEGORIZE,
            timeout_s=timeout_s,
        )
        if not isinstance(body, dict) or body.get("status") != "success":
            raise ToolError(f"logs.search: Loki returned {body!r:.200}")  # noqa: TRY003
        data = body.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "streams":
            raise ToolError("logs.search: a log query must return streams, not a metric")  # noqa: TRY003
        entries: list[tuple[object, str, Mapping[str, object]]] = []
        for stream in data.get("result") or []:
            if not isinstance(stream, dict):
                continue
            raw_labels = stream.get("stream")
            labels: Mapping[str, object] = raw_labels if isinstance(raw_labels, dict) else {}
            for value in stream.get("values") or []:
                if isinstance(value, list) and len(value) >= 2:  # noqa: PLR2004
                    entries.append((value[0], str(value[1]), labels))
        return streams_result(query, window, entries)


__all__ = ["SCHEMA", "LokiSearchTool", "render_logql", "streams_result"]
