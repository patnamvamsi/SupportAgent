"""WP-107: the tool registry, argument binding and log-path safety (ARCH §7).

Canonical test IDs from 11-wp §WP-107 that live here:
``test_tool_registry_rejects_class_tier_mismatch``, ``test_logs_path_outside_allowlist_denied``,
``test_tool_args_bound_from_service_only``, ``test_tool_call_event_recorded``.
``test_tool_result_matches_declared_schema`` is the contract suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.fakes.faults import FaultInjectingTool
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.redact import Redactor
from supportagent.adapters.tools.logs import LogsTool
from supportagent.app.tools import ToolRegistry
from supportagent.domain.errors import ConfigError, DomainValidationError, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.tools import bind_args, schema_errors, schema_fields
from supportagent.domain.trace import TraceContext
from supportagent.ports.tools import ToolResult

_REDACTOR = Redactor(detectors=built_in_detectors())
SERVICE = ServiceEntry(
    name="payroll-api",
    repos=["github:acme/payroll-api"],
    log_paths=["payroll-api"],
    dags=["nightly-sync"],
)
TRACE = TraceContext.new("acme", "tr_tools", "span_rca")


class _Echo:
    name = "echo.read"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema: Mapping[str, object] = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    literal_args = frozenset({"note"})

    def __init__(self, data: Mapping[str, object] | None = None) -> None:
        self.data = data

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        return ToolResult(self.data if self.data is not None else {"value": str(args)})


def _registry(tools: list[Any], events: Any, ids: Any, clock: Any) -> ToolRegistry:
    return ToolRegistry(
        tools, events=events, ids=ids, clock=clock, redact=lambda t: _REDACTOR.redact(t).text
    )


def test_tool_registry_rejects_class_tier_mismatch(events: Any, ids: Any, clock: Any) -> None:
    class _ReadAtT2(_Echo):
        tier = Tier.T2

    class _StateAtT0(_Echo):
        trust_class = TrustClass.STATE_CHANGING

    for bad in (_ReadAtT2(), _StateAtT0()):
        with pytest.raises(ConfigError, match="can't hold tier"):
            _registry([bad], events, ids, clock)
    with pytest.raises(ConfigError, match="twice"):
        _registry([_Echo(), _Echo()], events, ids, clock)


def test_tool_args_bound_from_service_only() -> None:
    bound = bind_args(
        {"repo": "$service.repos[0]", "limit": 5, "note": "reviewed literal"},
        SERVICE,
        literal_args=frozenset({"note"}),
    )
    assert bound == {"repo": "github:acme/payroll-api", "limit": 5, "note": "reviewed literal"}
    assert bind_args({"dags": "$service.dags"}, SERVICE, literal_args=frozenset()) == {
        "dags": ["nightly-sync"]
    }
    for template in (
        {"repo": "evil/repo"},  # a free string (e.g. from a ticket body)
        {"repo": "$case.subject"},  # only $service.* references exist
        {"repo": "$service.repos[3]"},  # out of range
        {"repo": "$service.secret_field"},  # not a catalog field
        {"repo": "$service.oncall_schedule"},  # empty on this service
        {"flag": True},
    ):
        with pytest.raises(DomainValidationError):
            bind_args(template, SERVICE, literal_args=frozenset())


def test_logs_path_outside_allowlist_denied(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    (root / "payroll-api").mkdir(parents=True)
    (root / "payroll-api" / "app.log").write_text("ok\nERROR timeout\n", encoding="utf-8")
    (tmp_path / "secret.log").write_text("ERROR root password\n", encoding="utf-8")
    (root / "linked").symlink_to(tmp_path)
    tool = LogsTool(root=root, allowlist=frozenset({"payroll-api", "../secret.log", "linked"}))
    good = tool.run({"path": "payroll-api", "pattern": "ERROR"}, timeout_s=1)
    assert good.data["count"] == 1
    for path in ("other-service", "../secret.log", "linked"):
        with pytest.raises(ToolError, match=r"allowlisted|outside"):
            tool.run({"path": path, "pattern": "ERROR"}, timeout_s=1)


def test_logs_line_cap(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "big.log").write_text("ERROR x\n" * 1500, encoding="utf-8")
    result = LogsTool(root=root, allowlist=frozenset({"svc"})).run(
        {"path": "svc", "pattern": "ERROR", "max_lines": 5000}, timeout_s=1
    )
    assert result.data["count"] == 1000 and result.truncated


def test_tool_call_event_recorded(events: Any, ids: Any, clock: Any) -> None:
    registry = _registry([_Echo()], events, ids, clock)
    registry.call("echo.read", {"note": "caller support+ticket@acme-corp.example"}, SERVICE, TRACE)
    (event,) = [e for e in events.appended if e.kind is EventKind.TOOL_CALL]
    assert (event.payload["tool"], event.status, event.trace_id) == ("echo.read", "ok", "tr_tools")
    assert "acme-corp.example" not in str(event.payload["args"]), "args are redacted"


def test_off_schema_result_is_a_tool_error(events: Any, ids: Any, clock: Any) -> None:
    registry = _registry([_Echo({"value": 42})], events, ids, clock)
    with pytest.raises(ToolError, match="declared schema"):
        registry.call("echo.read", {}, SERVICE, TRACE)
    assert [e.status for e in events.appended] == ["error"]
    with pytest.raises(ToolError, match="not registered"):
        registry.call("nope.tool", {}, SERVICE, TRACE)


def test_gha_500_fault(events: Any, ids: Any, clock: Any) -> None:
    registry = _registry([FaultInjectingTool(_Echo())], events, ids, clock)
    with pytest.raises(ToolError) as caught:
        registry.call("echo.read", {}, SERVICE, TRACE)
    assert caught.value.retryable


def test_schema_subset() -> None:
    schema = {
        "type": "object",
        "required": ["n"],
        "properties": {
            "n": {"type": "integer"},
            "xs": {"type": "array", "maxItems": 2, "items": {"type": "string"}},
        },
    }
    assert schema_errors({"n": 1, "xs": ["a"]}, schema) == []
    assert len(schema_errors({"xs": ["a", "b", 3]}, schema)) == 3
    assert schema_errors({"n": True}, schema) == ["$.n: expected integer, got bool"]
    assert schema_fields(schema) == {"result.n", "result.xs"}
