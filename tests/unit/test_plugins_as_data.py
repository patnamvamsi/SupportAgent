"""ADR-0016: a plugin is data. A capability, a graph extractor and a CMDB class
come from the manifest, with no core code: a toy ``mongodb`` plugin proves each.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.adapters.files.plugin_loader import LoadedPlugin
from supportagent.adapters.tools.mcp import McpBoundTool, McpHttpClient
from supportagent.bootstrap import merge_capabilities
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.graph import EdgeFact, NodeFact
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.model.plugin import (
    CapabilitySpec,
    Extractor,
    ExtractRule,
    PluginManifest,
)
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.trace import TraceContext
from tests.unit.test_mcp_binding import FakeMcpServer, _registry, _text

pytestmark = pytest.mark.unit

_SLOW_OPS: dict[str, object] = {
    "type": "object",
    "required": ["database", "ops", "max_ms"],
    "properties": {
        "database": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}},
        "max_ms": {"type": "number"},
    },
}


def _plugin(**provides: Any) -> LoadedPlugin:
    manifest = PluginManifest.model_validate(
        {
            "id": "mongodb",
            "version": "0.1.0",
            "description": "MongoDB",
            "core_api": ">=1.0,<2",
            "provides": provides,
            "detectors": {"signals": [{"files": "**/package.json", "contains": "mongoose"}]},
        }
    )
    return LoadedPlugin(manifest, Path("plugins/mongodb"), "0" * 64)


_MONGO = {
    "capabilities": {
        "mongodb.slow_ops": {"description": "Slow operations", "result_schema": _SLOW_OPS}
    },
    "backends": [{"capability": "mongodb.*", "mcp": {"image": "x/mongo-mcp:1", "tools": ["slow"]}}],
}


def test_a_declared_capability_joins_the_catalog() -> None:
    catalog = merge_capabilities([_plugin(**_MONGO)])
    assert catalog["mongodb.slow_ops"].result_schema == _SLOW_OPS
    assert "metrics.query" in catalog  # the built-in ones stay


def test_a_plugin_cannot_redefine_a_built_in_or_disagree_with_another() -> None:
    clash = {"capabilities": {"metrics.query": {"description": "x", "result_schema": _SLOW_OPS}}}
    with pytest.raises(ConfigError, match="built-in capability"):
        merge_capabilities([_plugin(**clash)])
    other = json.loads(json.dumps(_MONGO))
    other["capabilities"]["mongodb.slow_ops"]["result_schema"]["required"] = ["database"]
    with pytest.raises(ConfigError, match="differently"):
        merge_capabilities([_plugin(**_MONGO), _plugin(**other)])


@pytest.mark.parametrize(
    ("schema", "why"),
    [
        ({"type": "array"}, "object schema"),
        ({"type": "object", "properties": {}, "required": ["x"]}, "every required key"),
    ],
)
def test_a_result_schema_must_be_an_object_with_its_required_keys(
    schema: dict[str, object], why: str
) -> None:
    with pytest.raises(ValidationError, match=why):
        CapabilitySpec(description="d", result_schema=schema)


def test_an_mcp_server_serves_a_declared_capability_and_is_held_to_its_schema() -> None:
    capability = merge_capabilities([_plugin(**_MONGO)])["mongodb.slow_ops"]
    replies: list[Mapping[str, Any]] = [
        {"database": "orders", "ops": [{"op": "find", "ms": 4200}], "max_ms": 4200},
        {"database": "orders"},  # off-schema: missing ops and max_ms
    ]
    server = FakeMcpServer({"slow": lambda _args: _text(replies.pop(0))})
    client = McpHttpClient(
        server="mongo",
        url="http://mongo-mcp/mcp",
        client=httpx.Client(transport=server.transport()),
        declared=frozenset({"slow"}),
    )
    tool = McpBoundTool(
        capability,
        client=client,
        tool="slow",
        profile="passthrough",
        fixed_args={},
        trust_class=TrustClass.READ,
        tier=Tier.T0,
    )
    registry = _registry([tool])
    service = ServiceEntry(name="orders-api", databases=["orders"])
    trace = TraceContext.new("acme", "tr", "sp")
    template = {"database": "$service.databases[0]"}
    assert registry.call("mongodb.slow_ops", template, service, trace).data["max_ms"] == 4200
    with pytest.raises(ToolError, match="declared schema"):
        registry.call("mongodb.slow_ops", template, service, trace)


_NODE_RULES = Extractor.model_validate(
    {
        "match": "**/package.json",
        "format": "json",
        "rules": [
            {
                "key": "^name$",
                "own": True,
                "node": {"id": "service:{name}", "kind": "Service", "tech": ["nodejs"]},
            },
            {"key": "^dependencies\\.express$", "value": ".", "tech": ["express"]},
        ],
    }
)
_CONFIG_RULES = Extractor.model_validate(
    {
        "match": "**/config/*.json",
        "format": "json",
        "rules": [
            {
                "key": "mongo\\w*\\.uri$",
                "value": "mongodb(?:\\+srv)?://[^/]+/(?P<name>[A-Za-z0-9_-]+)",
                "node": {"id": "database:{name}", "kind": "Database", "tech": ["mongodb"]},
                "edge": "uses",
            },
            {
                "key": "url$",
                "value": "^https?://(?P<name>[a-z0-9-]+)\\.[a-z0-9-]+\\.svc",
                "edge": "calls",
                "to": "service:{name}",
            },
        ],
    }
)
_ENV_RULES = Extractor.model_validate(
    {
        "match": "**/.env.example",
        "format": "env",
        "rules": [
            {
                "key": "^AMQP_URL$",
                "value": "amqps?://(?:[^@]*@)?(?P<name>[a-z0-9-]+)\\.",
                "node": {"id": "runtime:rabbitmq/{name}", "kind": "Runtime", "tech": ["rabbitmq"]},
                "edge": "uses",
            }
        ],
    }
)


def _facts(files: Mapping[str, str], declared: list[Extractor], cmdb: Any = None) -> set[Any]:
    facts = structured_facts(files, cmdb or {}, {}, [("toy", e) for e in declared])
    return {
        (f.id, f.kind, tuple(f.tech))
        if isinstance(f, NodeFact)
        else (f.src, f.kind, f.dst)
        if isinstance(f, EdgeFact)
        else None
        for f in facts
    }


def test_declared_rules_find_a_node_service_its_database_broker_and_calls() -> None:
    files = {
        "orders-api/package.json": json.dumps(
            {"name": "orders-api", "dependencies": {"express": "^4", "mongoose": "^8"}}
        ),
        "orders-api/config/default.json": json.dumps(
            {
                "mongo": {"uri": "mongodb+srv://orders-ro@cluster0.example.net/ordersdb"},
                "payments": {"url": "http://payments-api.shop.svc:8080"},
            }
        ),
        "orders-api/.env.example": "# broker\nAMQP_URL=amqps://user@orders-mq.internal:5671/\n",
    }
    facts = _facts(files, [_NODE_RULES, _CONFIG_RULES, _ENV_RULES])
    assert ("service:orders-api", "Service", ("nodejs",)) in facts
    assert ("service:orders-api", "Service", ("express",)) in facts
    assert ("database:ordersdb", "Database", ("mongodb",)) in facts
    assert ("service:orders-api", "uses", "database:ordersdb") in facts
    assert ("service:orders-api", "calls", "service:payments-api") in facts
    assert ("service:orders-api", "uses", "runtime:rabbitmq/orders-mq") in facts


def test_a_declared_component_rule_never_overrides_a_built_in_one() -> None:
    pom = (
        Path(__file__).resolve().parents[2]
        / "evals/onboarding/system-a/sources/git/customer-service/pom.xml"
    ).read_text()
    files = {"crm/pom.xml": pom, "crm/package.json": json.dumps({"name": "billing-ui"})}
    facts = _facts(files, [_NODE_RULES])
    assert ("service:customer-service", "Service", ("java", "spring-boot")) in facts
    assert not any(f and f[0] == "service:billing-ui" for f in facts)


def test_a_declared_cmdb_class_becomes_its_node() -> None:
    cmdb = {
        "cis": [
            {
                "sys_id": "c1",
                "name": "ORDERSDB",
                "sys_class_name": "u_cmdb_ci_mongodb",
                "install_status": "Installed",
            }
        ]
    }
    declared = Extractor.model_validate(
        {
            "match": "cmdb:u_cmdb_ci_mongodb",
            "node": {"id": "database:{name}", "kind": "Database", "tech": ["mongodb"]},
        }
    )
    assert ("database:ordersdb", "Database", ("mongodb",)) in _facts({}, [declared], cmdb)
    assert not any(f and f[0] == "database:ordersdb" for f in _facts({}, [], cmdb))


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ({"key": "x", "edge": "deletes", "to": "service:{name}"}, "is not one of"),
        ({"key": "x", "value": "(?P<db>.+)", "to": "service:{name}", "edge": "calls"}, "lacks"),
        ({"key": "x", "own": True}, "own rule"),
        ({"key": "x"}, "yields"),
        ({"key": "(", "tech": ["x"]}, "bad regex"),
    ],
)
def test_a_bad_rule_does_not_load(rule: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError, match=why):
        ExtractRule.model_validate(rule)


def test_an_extractor_is_built_in_or_declared_not_both() -> None:
    with pytest.raises(ValidationError, match="built-in"):
        Extractor.model_validate({"match": "**/x.json"})
    with pytest.raises(ValidationError, match="file glob and a format"):
        Extractor.model_validate({"match": "**/x.json", "rules": [{"key": "a", "tech": ["t"]}]})
    with pytest.raises(ValidationError, match="cmdb"):
        Extractor.model_validate(
            {"match": "**/x.json", "node": {"id": "database:{name}", "kind": "Database"}}
        )
