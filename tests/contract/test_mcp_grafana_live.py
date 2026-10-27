"""WP-M06 contract: ``metrics.query`` on a real ``mcp-grafana`` equals the first-party adapter.

Both backends query the same Prometheus: the first-party adapter directly,
the binding through Grafana's MCP server (``query_prometheus``). Opt-in, as it
needs the stack in ``docker/observability`` (see its README):

    SUPPORTAGENT_MCP_GRAFANA_URL=http://<host>:8810/mcp \\
    SUPPORTAGENT_MCP_GRAFANA_TOKEN=... \\
    SUPPORTAGENT_PROMETHEUS_URL=http://<host>:9390 \\
    uv run pytest -m contract tests/contract/test_mcp_grafana_live.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.mcp import McpBoundTool, McpHttpClient, verify_server
from supportagent.adapters.tools.prometheus import PrometheusQueryTool
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.tools import schema_errors

_ENV = (
    "SUPPORTAGENT_MCP_GRAFANA_URL",
    "SUPPORTAGENT_MCP_GRAFANA_TOKEN",
    "SUPPORTAGENT_PROMETHEUS_URL",
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.enable_socket,
    pytest.mark.skipif(
        not all(os.environ.get(k) for k in _ENV), reason="needs the observability stack"
    ),
]

_QUERIES = [
    {"query": 'up{service="{{service}}"}', "service": "demo-api"},
    {"query": 'count by (job) (up{service=~"demo-.*"})'},
    {"query": 'prometheus_build_info{service="{{service}}"}', "service": "demo-api"},
]


def _bound() -> McpBoundTool:
    http = httpx.Client(
        headers={"Authorization": f"Bearer {os.environ['SUPPORTAGENT_MCP_GRAFANA_TOKEN']}"}
    )
    client = McpHttpClient(
        server="grafana",
        url=os.environ["SUPPORTAGENT_MCP_GRAFANA_URL"],
        client=http,
        declared=frozenset({"query_prometheus"}),
    )
    assert verify_server(client, TrustClass.READ, timeout_s=10) == []
    return McpBoundTool(
        CAPABILITIES["metrics.query"],
        client=client,
        tool="query_prometheus",
        profile="grafana.query_prometheus",
        fixed_args={"datasourceUid": "prometheus"},
        trust_class=TrustClass.READ,
        tier=Tier.T0,
    )


def _labels_and_values(data: object) -> list[tuple[tuple[tuple[str, str], ...], float]]:
    assert isinstance(data, dict)
    return sorted((tuple(sorted(s["labels"].items())), s["value"]) for s in data["series"])


@pytest.mark.parametrize("args", _QUERIES, ids=["up", "count-by-job", "build-info"])
def test_mcp_grafana_matches_first_party_prometheus(args: dict[str, object]) -> None:
    mcp = _bound().run(args, timeout_s=10)
    first = PrometheusQueryTool(
        client=httpx.Client(base_url=os.environ["SUPPORTAGENT_PROMETHEUS_URL"])
    ).run(args, timeout_s=10)
    assert schema_errors(dict(mcp.data), CAPABILITIES["metrics.query"].result_schema) == []
    assert mcp.data["query"] == first.data["query"]
    assert mcp.data["count"] == first.data["count"]
    assert int(str(mcp.data["count"])) > 0
    # The same series; values are gauges sampled a moment apart, so compare the labels.
    assert [k for k, _ in _labels_and_values(mcp.data)] == [
        k for k, _ in _labels_and_values(first.data)
    ]
