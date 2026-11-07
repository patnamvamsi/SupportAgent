"""ADR-0021 §3-§5: the tracer looks one object up stage by stage.

Lookups go through the tool registry (bound, schema-checked, audited). The
verdict comes from the raw results; what the model may see is the allowed
fields, pseudonymised. A stage that can't be looked up is unknown, never
missing, and the traced id can never change a query's shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from supportagent.app.tools import ToolRegistry
from supportagent.app.tracing import FlowTracer
from supportagent.domain.errors import DomainValidationError, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.flow import Flow
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.trace import TraceContext
from supportagent.ports.tools import ToolResult
from tests.unit.test_pii import BrokenAnalyzer, RegexAnalyzer

pytestmark = pytest.mark.unit

TRACE = TraceContext.new("acme", "tr_trace", "span_trace")
_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": ["matches"],
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["ts", "line"],
                "properties": {"ts": {"type": "string"}, "line": {"type": "string"}},
            },
        }
    },
}


class FakeLogs:
    """``logs.search`` answering from a table of query -> lines."""

    name = "logs.search"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = _SCHEMA
    literal_args = frozenset({"query"})

    def __init__(self, lines: Mapping[str, list[tuple[str, str]]], fail: str = "") -> None:
        self.lines = lines
        self.fail = fail
        self.queries: list[str] = []

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        query = str(args["query"])
        self.queries.append(query)
        if self.fail and self.fail in query:
            raise ToolError("loki: 503")  # noqa: TRY003
        rows = self.lines.get(query, [])
        return ToolResult({"matches": [{"ts": ts, "line": line} for ts, line in rows]})


def _stage(app: str, *, within: int | None = 30, **extra: Any) -> dict[str, Any]:
    return {
        "id": app,
        "title": app.title(),
        "component": f"service:{app}",
        "lookup": {
            "capability": "logs.search",
            "source": "logs",
            "args": {"query": f'{{app="{app}"}} |= "$object.id"'},
            "found": "matches[]",
            "at": "matches[].ts",
            "fields": ["matches[].ts", "matches[].line"],
        },
        "within_min": within,
        **extra,
    }


def _flow(**over: Any) -> Flow:
    body: dict[str, Any] = {
        "flow": "trade-settlement",
        "title": "Trade from submission to settlement",
        "object": "trade",
        "id_pattern": r"T-\d{4}",
        "reviewed": True,
        "stages": [
            _stage("submit", within=None),
            _stage("enrich", rejection={"path": "matches[].line", "values": ["REJECTED"]}),
            _stage("settle", terminal=True),
        ],
    }
    body.update(over)
    return Flow.model_validate(body)


def _tracer(logs: FakeLogs, events: Any, ids: Any, clock: Any, pii: Any = None) -> FlowTracer:
    registry = ToolRegistry([logs], events=events, ids=ids, clock=clock, redact=lambda t: t)
    catalog = Catalog(version="1", services=[ServiceEntry(name="submit")])
    return FlowTracer(tools=registry, catalog=catalog, pii=pii, events=events, ids=ids, clock=clock)


def _q(app: str, oid: str = "T-1234") -> str:
    return f'{{app="{app}"}} |= "{oid}"'


def test_stuck_at_settlement(events: Any, ids: Any, clock: Any) -> None:
    logs = FakeLogs(
        {
            _q("submit"): [("2026-10-20T07:00:00Z", "trade T-1234 submitted by Jane Doe")],
            _q("enrich"): [("2026-10-20T07:05:00Z", "trade T-1234 enriched")],
        }
    )
    run = _tracer(logs, events, ids, clock, RegexAnalyzer()).trace(
        _flow(), "T-1234", TRACE, actor="cli"
    )
    assert (run.verdict.kind, run.verdict.stage) == ("stuck", "settle")
    assert run.verdict.since == datetime(2026, 10, 20, 7, 5, tzinfo=UTC)
    # The model sees the allowed fields, pseudonymised; the traced id is kept.
    line = run.evidence["submit"]["matches"][0]["line"]  # type: ignore[index]
    assert line == "trade T-1234 submitted by <PERSON_1>"
    assert run.pii_counts == {"PERSON": 1}
    # Every lookup is an audited tool call, and the trace is one event without PII.
    kinds = [e.kind for e in events.appended]
    assert kinds.count(EventKind.TOOL_CALL) == 3
    audit = events.appended[-1]
    assert audit.kind == EventKind.TRACE and audit.actor == "cli"
    assert audit.payload["verdict"] == "stuck" and audit.payload["deviation"] == "settle"
    assert audit.payload["pii_counts"] == {"PERSON": 1}
    assert "Jane" not in repr(audit.payload)


def test_rejected_on_purpose(events: Any, ids: Any, clock: Any) -> None:
    logs = FakeLogs(
        {
            _q("submit"): [("2026-10-20T07:00:00Z", "ok")],
            _q("enrich"): [("2026-10-20T07:01:00Z", "REJECTED")],
        }
    )
    run = _tracer(logs, events, ids, clock, RegexAnalyzer()).trace(
        _flow(), "T-1234", TRACE, actor="cli"
    )
    assert (run.verdict.kind, run.verdict.stage) == ("rejected", "enrich")


def test_failed_lookup_is_unknown_not_missing(events: Any, ids: Any, clock: Any) -> None:
    logs = FakeLogs({_q("submit"): [("2026-10-20T07:00:00Z", "ok")]}, fail='app="enrich"')
    run = _tracer(logs, events, ids, clock, RegexAnalyzer()).trace(
        _flow(), "T-1234", TRACE, actor="cli"
    )
    assert (run.verdict.kind, run.verdict.stage) == ("cannot_tell", "enrich")
    assert "loki: 503" in run.verdict.reason


def test_keys_from_an_earlier_stage(events: Any, ids: Any, clock: Any) -> None:
    settle = _stage("settle", terminal=True)
    settle["lookup"]["args"] = {"query": '{app="settle"} |= "$key.ref"'}
    flow = _flow(
        keys=[{"name": "ref", "stage": "submit", "field": "matches[].line"}],
        stages=[_stage("submit", within=None), settle],
    )
    logs = FakeLogs(
        {
            _q("submit"): [("2026-10-20T07:00:00Z", "BK-77")],
            '{app="settle"} |= "BK-77"': [("2026-10-20T07:10:00Z", "settled")],
        }
    )
    run = _tracer(logs, events, ids, clock, RegexAnalyzer()).trace(
        flow, "T-1234", TRACE, actor="cli"
    )
    assert run.verdict.kind == "complete"


def test_missing_key_is_unknown(events: Any, ids: Any, clock: Any) -> None:
    settle = _stage("settle", terminal=True)
    settle["lookup"]["args"] = {"query": '{app="settle"} |= "$key.ref"'}
    flow = _flow(
        keys=[{"name": "ref", "stage": "submit", "field": "matches[].line"}],
        stages=[_stage("submit", within=None), settle],
    )
    logs = FakeLogs({_q("submit"): [("2026-10-20T07:00:00Z", 'not a "safe" value')]})
    run = _tracer(logs, events, ids, clock, RegexAnalyzer()).trace(
        flow, "T-1234", TRACE, actor="cli"
    )
    assert (run.verdict.kind, run.verdict.stage) == ("cannot_tell", "settle")
    assert "key.ref" in run.verdict.reason


def test_unsafe_or_foreign_ids_are_refused(events: Any, ids: Any, clock: Any) -> None:
    tracer = _tracer(FakeLogs({}), events, ids, clock, RegexAnalyzer())
    with pytest.raises(DomainValidationError):
        tracer.trace(_flow(), 'T-1234"} |= "', TRACE, actor="cli")
    with pytest.raises(DomainValidationError):
        tracer.trace(_flow(), "X-1", TRACE, actor="cli")


def test_unreviewed_draft_is_refused(events: Any, ids: Any, clock: Any) -> None:
    tracer = _tracer(FakeLogs({}), events, ids, clock, RegexAnalyzer())
    with pytest.raises(DomainValidationError, match="unreviewed"):
        tracer.trace(_flow(reviewed=False), "T-1234", TRACE, actor="cli")


def test_reference_in_a_bound_argument_is_unknown(events: Any, ids: Any, clock: Any) -> None:
    stage = _stage("submit", within=None)
    stage["lookup"]["args"] = {"service": "$object.id"}
    flow = _flow(stages=[stage, _stage("settle", terminal=True)])
    run = _tracer(FakeLogs({}), events, ids, clock, RegexAnalyzer()).trace(
        flow, "T-1234", TRACE, actor="cli"
    )
    assert run.verdict.kind == "cannot_tell"
    assert "literally" in run.verdict.reason


def test_pii_unavailable_keeps_the_timeline_and_passes_nothing(
    events: Any, ids: Any, clock: Any
) -> None:
    logs = FakeLogs({_q("submit"): [("2026-10-20T07:00:00Z", "Jane Doe")]})
    run = _tracer(logs, events, ids, clock, BrokenAnalyzer()).trace(
        _flow(), "T-1234", TRACE, actor="cli"
    )
    assert run.verdict.kind == "stuck"
    assert run.evidence == {}
    assert run.pii_error is not None and "supportagent[pii]" in run.pii_error


def test_no_analyser_passes_nothing(events: Any, ids: Any, clock: Any) -> None:
    logs = FakeLogs({_q("submit"): [("2026-10-20T07:00:00Z", "Jane Doe")]})
    run = _tracer(logs, events, ids, clock, None).trace(_flow(), "T-1234", TRACE, actor="cli")
    assert run.evidence == {}
    assert run.pii_error == "no PII analyser is configured (pii.engine: off)"
    assert run.as_dict()["verdict"]["kind"] == "stuck"  # type: ignore[index]
