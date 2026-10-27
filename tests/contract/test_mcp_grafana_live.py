"""WP-M06 / BL-050 contract: ``mcp-grafana`` bindings equal the first-party adapters.

Both backends query the same Prometheus: the first-party adapter directly,
the binding through Grafana's MCP server (``query_prometheus``). Opt-in, as it
needs the stack in ``docker/observability`` (see its README):

    SUPPORTAGENT_MCP_GRAFANA_URL=http://<host>:8810/mcp \\
    SUPPORTAGENT_MCP_GRAFANA_TOKEN=... \\
    SUPPORTAGENT_PROMETHEUS_URL=http://<host>:9390 \\
    SUPPORTAGENT_LOKI_URL=http://<host>:3110 \\
    uv run pytest -m contract tests/contract/test_mcp_grafana_live.py
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from supportagent.adapters.system.clock import SystemClock
from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.loki import LokiSearchTool
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


# --- BL-050: logs.search over Loki --------------------------------------------

_LOKI = os.environ.get("SUPPORTAGENT_LOKI_URL")


def _push_marker(base: str) -> str:
    """One fresh line in Loki (the test's own write to the lab stack, never the agent's)."""
    marker = f"bl050-contract-{uuid.uuid4().hex[:12]}"
    stream = {"stream": {"service": "contract-api"}, "values": [[str(time.time_ns()), marker]]}
    httpx.post(
        f"{base}/loki/api/v1/push", json={"streams": [stream]}, timeout=10
    ).raise_for_status()
    return marker


@pytest.mark.skipif(not _LOKI, reason="needs SUPPORTAGENT_LOKI_URL (the stack's Loki)")
def test_mcp_grafana_loki_matches_first_party_loki() -> None:
    assert _LOKI is not None
    marker = _push_marker(_LOKI)
    args = {"query": f'{{service="{{{{service}}}}"}} |= "{marker}"', "service": "contract-api"}
    http = httpx.Client(
        headers={"Authorization": f"Bearer {os.environ['SUPPORTAGENT_MCP_GRAFANA_TOKEN']}"}
    )
    client = McpHttpClient(
        server="grafana",
        url=os.environ["SUPPORTAGENT_MCP_GRAFANA_URL"],
        client=http,
        declared=frozenset({"query_loki_logs"}),
    )
    assert verify_server(client, TrustClass.READ, timeout_s=10) == []
    bound = McpBoundTool(
        CAPABILITIES["logs.search"],
        client=client,
        tool="query_loki_logs",
        profile="grafana.query_loki_logs",
        fixed_args={"datasourceUid": "loki"},
        trust_class=TrustClass.READ,
        tier=Tier.T0,
    )
    first = LokiSearchTool(client=httpx.Client(base_url=_LOKI), clock=SystemClock())
    for _ in range(20):  # ingestion is asynchronous
        mcp = bound.run(args, timeout_s=10)
        if mcp.data["count"]:
            break
        time.sleep(0.5)
    direct = first.run(args, timeout_s=10)
    schema = CAPABILITIES["logs.search"].result_schema
    assert schema_errors(dict(mcp.data), schema) == []
    assert schema_errors(dict(direct.data), schema) == []
    assert mcp.data["count"] == direct.data["count"] == 1
    assert mcp.data["matches"] == direct.data["matches"]
