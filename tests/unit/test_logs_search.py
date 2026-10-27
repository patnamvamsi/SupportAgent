"""BL-050: ``logs.search`` over Loki, first-party and through ``mcp-grafana``.

The capability's schema, the reviewed-LogQL rules (``{{service}}`` from the
catalog, a selective stream selector, capped window and lines), and the same
result on either backend, so a pack check runs unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.loki import SCHEMA, LokiSearchTool, render_logql
from supportagent.domain.errors import ToolError
from supportagent.domain.tools import schema_errors
from tests.unit.test_mcp_binding import (
    _SERVICE,
    _TRACE,
    FakeMcpServer,
    _bound,
    _client,
    _registry,
    _text,
)

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_NS = 1_000_000_000
_TEMPLATE = {
    "query": '{service="{{service}}"} |~ "Name or service not known"',
    "service": "$service.name",
    "window_min": 30,
}
#: (seconds before now, line): the newest last, as Loki doesn't promise an order.
_LINES = [(120, "ERROR Name or service not known: payments.internal"), (5, "ERROR again")]


def _loki(seen: list[httpx.Request], lines: list[tuple[int, str]] = _LINES) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Loki 3 with categorize-labels: structured metadata rides as a third element.
        meta = {"structuredMetadata": {"detected_level": "error"}}
        values = [[str(int((_NOW.timestamp() - age) * _NS)), line, meta] for age, line in lines]
        body = {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": {"service": "orders-api"}, "values": values}],
            },
        }
        return httpx.Response(200, json=body)

    return httpx.Client(base_url="https://loki.test", transport=httpx.MockTransport(handler))


def _first_party(seen: list[httpx.Request] | None = None) -> LokiSearchTool:
    return LokiSearchTool(
        client=_loki(seen if seen is not None else []), clock=FrozenClock(start=_NOW)
    )


def test_logs_search_result_matches_declared_schema() -> None:
    seen: list[httpx.Request] = []
    registry = _registry([_first_party(seen)])
    result = registry.call("logs.search", _TEMPLATE, _SERVICE, _TRACE)
    assert schema_errors(result.data, SCHEMA) == []
    assert result.data["count"] == 2
    matches: list[Mapping[str, Any]] = result.data["matches"]  # type: ignore[assignment]
    assert [m["line"] for m in matches] == ["ERROR again", _LINES[0][1]]  # newest first
    assert matches[0]["ts"] == "2026-10-27T11:59:55Z"
    params = seen[0].url.params
    assert params["query"] == '{service="orders-api"} |~ "Name or service not known"'
    assert int(params["end"]) - int(params["start"]) == 30 * 60 * _NS
    assert (params["direction"], params["limit"]) == ("backward", "50")
    assert seen[0].headers["X-Loki-Response-Encoding-Flags"] == "categorize-labels"
    assert matches[0]["labels"] == {"service": "orders-api"}  # stream labels only
    assert CAPABILITIES["logs.search"].literal_args == {"query", "window_min", "limit"}


@pytest.mark.parametrize(
    ("query", "why"),
    [
        ("{}", "stream selector"),
        ('{job=~".*"} |= "x"', "stream selector"),
        ('|= "ERROR"', "stream selector"),
        ('{service="{{service}}"}', "needs service bound"),
    ],
)
def test_logs_search_rejects_unscoped_queries(query: str, why: str) -> None:
    with pytest.raises(ToolError, match=why):
        render_logql({"query": query, "service": "Not A Catalog Name"})


def test_logs_search_caps_window_lines_and_length() -> None:
    with pytest.raises(ToolError, match="window_min"):
        render_logql({"query": '{a="b"}', "window_min": 2000})
    with pytest.raises(ToolError, match="limit"):
        render_logql({"query": '{a="b"}', "limit": 500})
    long_lines = [(i, "x" * 900) for i in range(120)]
    result = LokiSearchTool(client=_loki([], long_lines), clock=FrozenClock(start=_NOW)).run(
        {"query": '{a="b"}'}, timeout_s=2
    )
    assert result.truncated
    assert result.data["count"] == 100
    matches: list[Mapping[str, Any]] = result.data["matches"]  # type: ignore[assignment]
    assert {len(str(m["line"])) for m in matches} == {500}


def _loki_mcp(args: Mapping[str, Any]) -> dict[str, Any]:
    """``mcp-grafana`` ``query_loki_logs`` as observed live (BL-050): text JSON entries."""
    data = [
        {
            "timestamp": str(int((_NOW.timestamp() - age) * _NS)),
            "line": line,
            "labels": {"service": "orders-api"},
            "structuredMetadata": {"detected_level": "error"},
        }
        for age, line in _LINES
    ]
    return _text({"data": data, "metadata": {"linesReturned": len(data)}})


def test_same_logs_result_first_party_and_mcp() -> None:
    server = FakeMcpServer({"query_loki_logs": _loki_mcp})
    bound = _bound(
        _client(server, {"query_loki_logs"}),
        tool="query_loki_logs",
        profile="grafana.query_loki_logs",
        fixed_args={"datasourceUid": "loki"},
        capability=CAPABILITIES["logs.search"],
    )
    via_mcp = _registry([bound]).call("logs.search", _TEMPLATE, _SERVICE, _TRACE)
    first_party = _registry([_first_party()]).call("logs.search", _TEMPLATE, _SERVICE, _TRACE)
    assert via_mcp.data == first_party.data
    (_, sent) = server.calls[-1]
    assert sent["logql"] == '{service="orders-api"} |~ "Name or service not known"'
    assert (sent["startRfc3339"], sent["endRfc3339"], sent["limit"]) == ("now-30m", "now", 50)
    assert sent["datasourceUid"] == "loki"
