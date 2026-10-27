"""WP-M06: tool bindings and the MCP client's registration contract.

The server here is an in-memory JSON-RPC fake on ``httpx.MockTransport``; the
integration suite runs the same client against the reference MCP SDK server,
and the contract suite against a real ``mcp-grafana``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from supportagent.adapters.fakes.events import RecordingEventSink
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.mcp import McpBoundTool, McpHttpClient
from supportagent.app.tools import ToolRegistry
from supportagent.bootstrap import KNOWN_TOOL_SCHEMAS, build_tool_registry
from supportagent.config.schema import McpServer, Tools
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.trace import TraceContext
from tests.unit.test_triage_service import _shadow_config

_REPO = Path(__file__).resolve().parents[2]
_URL = "http://mcp.test/mcp"
_VECTOR = [
    {"metric": {"__name__": "up", "service": "orders-api"}, "value": [1.0, "1"]},
    {"metric": {"__name__": "up", "service": "orders-api", "pod": "b"}, "value": [1.0, "0"]},
]


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 27, tzinfo=UTC)


Handler = Callable[[Mapping[str, Any]], dict[str, Any]]


class FakeMcpServer:
    """A streamable-HTTP MCP server: sessions, ``tools/list``, ``tools/call``, SSE replies."""

    def __init__(
        self,
        tools: Mapping[str, Handler],
        *,
        annotations: Mapping[str, Mapping[str, bool]] | None = None,
        sse: bool = False,
    ) -> None:
        self.tools = dict(tools)
        self.annotations = dict(annotations or {})
        self.sse = sse
        self.methods: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions = 0
        self.expire_next = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 — one per method
        body = json.loads(request.content)
        method = body.get("method", "")
        self.methods.append(method)
        if method == "initialize":
            self.sessions += 1
            return self._reply(
                body,
                {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}},
                {"Mcp-Session-Id": f"s{self.sessions}"},
            )
        if request.headers.get("mcp-session-id") != f"s{self.sessions}" or self.expire_next:
            self.expire_next = False
            return httpx.Response(404)
        if "id" not in body:
            return httpx.Response(202)
        if method == "tools/list":
            listed = [
                {"name": n, "inputSchema": {"type": "object"}, "annotations": self.annotations[n]}
                if n in self.annotations
                else {"name": n, "inputSchema": {"type": "object"}}
                for n in self.tools
            ]
            return self._reply(body, {"tools": listed})
        if method == "tools/call":
            name, args = body["params"]["name"], body["params"]["arguments"]
            self.calls.append((name, args))
            if name not in self.tools:
                return self._reply(body, None, error={"code": -32602, "message": "unknown tool"})
            return self._reply(body, self.tools[name](args))
        return self._reply(body, None, error={"code": -32601, "message": "no such method"})

    def _reply(
        self,
        body: Mapping[str, Any],
        result: object,
        headers: Mapping[str, str] | None = None,
        error: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": body["id"]}
        message["error" if error else "result"] = error or result
        if self.sse:
            text = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            text += f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(
                200,
                text=text,
                headers={"content-type": "text/event-stream", **(headers or {})},
            )
        return httpx.Response(200, json=message, headers=dict(headers or {}))


def _text(payload: object) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _grafana(args: Mapping[str, Any]) -> dict[str, Any]:
    return _text({"data": _VECTOR})


def _client(server: FakeMcpServer, declared: set[str]) -> McpHttpClient:
    http = httpx.Client(transport=server.transport())
    return McpHttpClient(server="grafana", url=_URL, client=http, declared=frozenset(declared))


def _bound(client: McpHttpClient, **overrides: Any) -> McpBoundTool:
    spec: dict[str, Any] = {
        "client": client,
        "tool": "query_prometheus",
        "profile": "grafana.query_prometheus",
        "fixed_args": {"datasourceUid": "prometheus"},
        "trust_class": TrustClass.READ,
        "tier": Tier.T0,
    }
    spec.update(overrides)
    capability = spec.pop("capability", CAPABILITIES["metrics.query"])
    return McpBoundTool(capability, **spec)


def _registry(tools: list[Any]) -> ToolRegistry:
    return ToolRegistry(
        tools,
        events=RecordingEventSink(),
        ids=SequentialIdGenerator(),
        clock=_Clock(),
        redact=lambda text: text,
    )


_SERVICE = ServiceEntry(name="orders-api", databases=["orders"])
_TRACE = TraceContext("acme", "tr_1", "rca")
_TEMPLATE = {"query": 'up{service="{{service}}"}', "service": "$service.name"}


# --- the capability catalog ----------------------------------------------------------


def test_every_capability_has_one_schema() -> None:
    assert set(CAPABILITIES) == set(KNOWN_TOOL_SCHEMAS)
    assert CAPABILITIES["metrics.query"].literal_args == frozenset({"query"})


# --- the bound tool -------------------------------------------------------------------


def test_mcp_binding_serves_the_capability() -> None:
    server = FakeMcpServer({"query_prometheus": _grafana})
    result = _registry([_bound(_client(server, {"query_prometheus"}))]).call(
        "metrics.query", _TEMPLATE, _SERVICE, _TRACE
    )
    assert result.data["query"] == 'up{service="orders-api"}'
    assert result.data["count"] == 2 and result.data["max_value"] == 1.0
    # The capability's args became the tool's: the rendered PromQL plus the fixed uid.
    assert server.calls == [
        (
            "query_prometheus",
            {
                "queryType": "instant",
                "endTime": "now",
                "datasourceUid": "prometheus",
                "expr": 'up{service="orders-api"}',
            },
        )
    ]
    # Handshake once, verify once, then the call.
    assert server.methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]


def test_sse_replies_and_expired_sessions() -> None:
    server = FakeMcpServer({"query_prometheus": _grafana}, sse=True)
    registry = _registry([_bound(_client(server, {"query_prometheus"}))])
    assert registry.call("metrics.query", _TEMPLATE, _SERVICE, _TRACE).data["count"] == 2
    server.expire_next = True  # the server dropped the session: initialize again, once
    assert registry.call("metrics.query", _TEMPLATE, _SERVICE, _TRACE).data["count"] == 2
    assert server.sessions == 2


def test_undeclared_mcp_tool_denied() -> None:
    server = FakeMcpServer({"query_prometheus": _grafana, "grafana_api_request": _grafana})
    client = _client(server, {"query_prometheus"})
    with pytest.raises(ToolError, match="not declared"):
        client.call_tool("grafana_api_request", {"path": "/api/admin"}, timeout_s=1)
    assert server.methods == []  # refused before any request
    # A binding can't name an undeclared tool, in config or in code.
    with pytest.raises(ValidationError, match="not declared"):
        Tools.model_validate(
            {
                "mcp_servers": {"grafana": {"url": _URL, "tools": ["query_prometheus"]}},
                "bindings": {"metrics.query": {"mcp": "grafana", "tool": "grafana_api_request"}},
            }
        )
    with pytest.raises(ConfigError, match="not declared"):
        _bound(client, tool="grafana_api_request")
    # A declared tool the server doesn't list fails registration; nothing is called.
    missing = FakeMcpServer({})
    with pytest.raises(ToolError, match="declared but not listed"):
        _bound(_client(missing, {"query_prometheus"})).run(
            {"query": "up", "service": "x"}, timeout_s=1
        )
    assert missing.calls == []


def test_mcp_class_tier_mismatch_rejected() -> None:
    # Class and tier are fixed at registration and must agree (ARCH §7) ...
    with pytest.raises(ValidationError, match="can't hold tier"):
        McpServer.model_validate({"url": _URL, "tools": ["t"], "trust_class": "read", "tier": "T2"})
    # ... and only read registrations load until the action gate covers MCP.
    with pytest.raises(ValidationError, match="only read MCP tools"):
        McpServer.model_validate(
            {"url": _URL, "tools": ["t"], "trust_class": "execute", "tier": "T1"}
        )
    # The registry re-checks a bound tool built in code.
    server = FakeMcpServer({"query_prometheus": _grafana})
    with pytest.raises(ConfigError, match="can't hold tier"):
        _registry([_bound(_client(server, {"query_prometheus"}), tier=Tier.T2)])
    # A server that says the tool isn't read-only can't back a read registration.
    destructive = FakeMcpServer(
        {"query_prometheus": _grafana},
        annotations={"query_prometheus": {"readOnlyHint": False, "destructiveHint": True}},
    )
    with pytest.raises(ToolError, match="not read-only"):
        _bound(_client(destructive, {"query_prometheus"})).run(
            {"query": "up", "service": "x"}, timeout_s=1
        )
    assert destructive.calls == []


def test_binding_result_validated_against_capability_schema() -> None:
    def off_schema(args: Mapping[str, Any]) -> dict[str, Any]:
        return {"content": [], "structuredContent": {"series": "lots", "count": "two"}}

    server = FakeMcpServer({"activity": off_schema})
    tool = _bound(
        _client(server, {"activity"}),
        capability=CAPABILITIES["postgres.activity"],
        tool="activity",
        profile="passthrough",
        fixed_args={},
    )
    assert tool.result_schema is CAPABILITIES["postgres.activity"].result_schema
    with pytest.raises(ToolError, match="doesn't match its declared schema"):
        _registry([tool]).call(
            "postgres.activity", {"database": "$service.databases[0]"}, _SERVICE, _TRACE
        )


def test_mcp_tool_errors_are_tool_errors() -> None:
    def failing(args: Mapping[str, Any]) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "datasource down"}], "isError": True}

    server = FakeMcpServer({"query_prometheus": failing})
    with pytest.raises(ToolError, match="datasource down"):
        _registry([_bound(_client(server, {"query_prometheus"}))]).call(
            "metrics.query", _TEMPLATE, _SERVICE, _TRACE
        )

    def huge(args: Mapping[str, Any]) -> dict[str, Any]:
        return _text({"data": [{"metric": {"x": "y" * 2_000_000}, "value": [1, "1"]}]})

    big = FakeMcpServer({"query_prometheus": huge})
    with pytest.raises(ToolError, match="reply over"):
        _registry([_bound(_client(big, {"query_prometheus"}))]).call(
            "metrics.query", _TEMPLATE, _SERVICE, _TRACE
        )


# --- config and wiring ----------------------------------------------------------------


def _config(tools: Mapping[str, Any]) -> Any:
    base = _shadow_config()
    paths = base.paths.model_copy(
        update={"catalog": str(_REPO / "catalog" / "services.example.yaml")}
    )
    return base.model_copy(update={"tools": Tools.model_validate(tools), "paths": paths})


_BOUND_CONFIG: dict[str, Any] = {
    "prometheus": {"base_url": "http://prometheus:9090"},
    "mcp_servers": {"grafana": {"url": _URL, "token": "t-fake", "tools": ["query_prometheus"]}},
    "bindings": {
        "metrics.query": {
            "mcp": "grafana",
            "tool": "query_prometheus",
            "profile": "grafana.query_prometheus",
            "args": {"datasourceUid": "prometheus"},
        }
    },
}


def test_binding_replaces_the_first_party_adapter() -> None:
    server = FakeMcpServer({"query_prometheus": _grafana})
    registry = build_tool_registry(
        _config(_BOUND_CONFIG),
        events=RecordingEventSink(),
        ids=SequentialIdGenerator(),
        clock=_Clock(),
        transport=server.transport(),
    )
    tool = registry.get("metrics.query")
    assert isinstance(tool, McpBoundTool) and tool.backend == "mcp:grafana.query_prometheus"
    assert server.methods == []  # nothing is contacted until the first call
    assert registry.call("metrics.query", _TEMPLATE, _SERVICE, _TRACE).data["count"] == 2


def test_binding_to_an_unknown_capability_is_a_config_error() -> None:
    cfg = {
        **_BOUND_CONFIG,
        "bindings": {"traces.search": {"mcp": "grafana", "tool": "query_prometheus"}},
    }
    with pytest.raises(ConfigError, match="not a known capability"):
        build_tool_registry(
            _config(cfg), events=RecordingEventSink(), ids=SequentialIdGenerator(), clock=_Clock()
        )


def test_unbound_capability_skips_hypothesis() -> None:
    # No first-party adapter and no binding: the capability isn't registered, so a
    # runbook step that needs it can't run (and so can't support, test_rca).
    registry = build_tool_registry(
        _config({}), events=RecordingEventSink(), ids=SequentialIdGenerator(), clock=_Clock()
    )
    assert "metrics.query" not in registry.names
    from supportagent.bootstrap import build_rca_knowledge  # noqa: PLC0415
    from tests.unit.test_lab import _config as lab_config  # noqa: PLC0415

    knowledge = build_rca_knowledge(lab_config(), registry.names)
    assert "celery_backlog" not in knowledge.probes  # its only probe is a metrics query
    bound = build_rca_knowledge(lab_config(), registry.names | {"metrics.query"})
    assert "celery_backlog" in bound.probes


def test_mcp_token_resolved_from_its_env(tmp_path: Path) -> None:
    from supportagent.config.loader import load_config, load_config_offline  # noqa: PLC0415

    raw = (_REPO / "supportagent.example.yaml").read_text(encoding="utf-8")
    raw += (
        "tools:\n  mcp_servers:\n    grafana:\n      url: http://mcp:8000/mcp\n"
        "      token_env: MCP_GRAFANA_TOKEN\n      tools: [query_prometheus]\n"
    )
    path = tmp_path / "c.yaml"
    path.write_text(raw, encoding="utf-8")
    env = {
        "SUPPORTAGENT_DB_URL": "sqlite://",
        "SN_CLIENT_ID": "x",
        "SN_CLIENT_SECRET": "x",
        "OIDC_CLIENT_ID": "x",
        "OIDC_CLIENT_SECRET": "x",
        "SA_SESSION_SECRET": "x" * 40,
        "ANTHROPIC_API_KEY": "x",
        "PAGERDUTY_TOKEN": "x",
    }
    with pytest.raises(ConfigError, match="MCP_GRAFANA_TOKEN"):
        load_config(path, env)
    token = load_config(path, {**env, "MCP_GRAFANA_TOKEN": "t-fake"}).tools.mcp_servers["grafana"]
    assert token.token is not None and token.token.get_secret_value() == "t-fake"
    assert load_config_offline(path).tools.mcp_servers["grafana"].token is not None


# --- doctor -----------------------------------------------------------------------------


def test_mcp_doctor_check_reports_each_server() -> None:
    from dataclasses import replace  # noqa: PLC0415

    from supportagent.app.doctor.checks.endpoints import McpCheck  # noqa: PLC0415
    from supportagent.bootstrap import _mcp_probes  # noqa: PLC0415
    from tests.unit.test_doctor import _ctx  # noqa: PLC0415

    config = _config(_BOUND_CONFIG)
    base = replace(_ctx(clock=_Clock()), config=config)
    assert McpCheck().run(base).status == "pass"  # nothing registered in the context

    healthy = FakeMcpServer({"query_prometheus": _grafana})
    ok = McpCheck().run(replace(base, mcp_servers=_mcp_probes(config, healthy.transport())))
    assert (ok.status, ok.detail) == ("pass", "grafana serves metrics.query")
    assert healthy.methods == ["initialize", "notifications/initialized", "tools/list"]

    lacking = FakeMcpServer({})
    bad = McpCheck().run(replace(base, mcp_servers=_mcp_probes(config, lacking.transport())))
    assert bad.status == "fail" and "declared but not listed" in bad.detail

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    gone = McpCheck().run(replace(base, mcp_servers=_mcp_probes(config, httpx.MockTransport(down))))
    assert gone.status == "fail" and "unreachable" in gone.detail
