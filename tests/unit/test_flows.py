"""ADR-0021 §2, §3, §5: a flow file, and judging one traced object against it.

The verdict comes from tool results only, never the model: where the object
stopped, whether it is late, and "can't tell" (naming the stage) when the
deciding lookup couldn't run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from supportagent.domain.fieldpath import values_at
from supportagent.domain.flow_trace import StageObservation, judge
from supportagent.domain.model.flow import Flow

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)


def _stage(sid: str, *, within: int | None = 30, **extra: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "title": sid.title(),
        "component": f"service:{sid}",
        "lookup": {
            "capability": "logs.search",
            "source": "logs",
            "args": {"query": f'{{app="{sid}"}} |= "$object.id"'},
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


def _found(sid: str, minutes: int, *, rejected: bool = False) -> StageObservation:
    return StageObservation(sid, "found", T0 + timedelta(minutes=minutes), rejected=rejected)


def _seen(*obs: StageObservation) -> dict[str, StageObservation]:
    return {o.stage: o for o in obs}


# --- the flow file ------------------------------------------------------------


def test_flow_loads_and_accepts_only_safe_ids() -> None:
    flow = _flow()
    assert flow.accepts("T-1234")
    assert not flow.accepts("T-12")  # the flow's own pattern
    assert not flow.accepts('T-1234"} |= "x')  # never a query-shaped id


def test_records_lookup_needs_a_reason() -> None:
    stage = _stage("book", terminal=True)
    stage["lookup"] = {**stage["lookup"], "source": "records", "capability": "records.trade"}
    with pytest.raises(ValidationError, match="logs and messages"):
        _flow(stages=[_stage("submit"), stage])
    stage["lookup"]["records_reason"] = "the booking status is only in trades.status"
    assert _flow(stages=[_stage("submit"), stage]).reads_records()


def test_keys_come_from_earlier_stages() -> None:
    later = _stage("settle", terminal=True)
    later["lookup"] = {**later["lookup"], "args": {"query": '{app="s"} |= "$key.ref"'}}
    ok = _flow(
        keys=[{"name": "ref", "stage": "submit", "field": "matches[].line"}],
        stages=[_stage("submit"), later],
    )
    assert ok.stages[1].lookup.refs() == {"key.ref"}
    with pytest.raises(ValidationError, match="later stage"):
        _flow(
            keys=[{"name": "ref", "stage": "settle", "field": "matches[].line"}],
            stages=[_stage("submit"), later],
        )
    with pytest.raises(ValidationError, match="no key"):
        _flow(stages=[_stage("submit"), later])


def test_last_stage_must_be_terminal_and_ids_unique() -> None:
    with pytest.raises(ValidationError, match="terminal"):
        _flow(stages=[_stage("aa"), _stage("bb")])
    with pytest.raises(ValidationError, match="unique"):
        _flow(stages=[_stage("aa"), _stage("aa", terminal=True)])


def test_values_at_steps_into_lists() -> None:
    data = {"matches": [{"ts": "a"}, {"ts": "b"}], "rows": []}
    assert values_at(data, "matches[].ts") == ["a", "b"]
    assert values_at(data, "rows[]") == []
    assert values_at(data, "rows") == [[]]
    assert values_at(data, "absent[].x") == []


# --- the verdict --------------------------------------------------------------


def test_complete() -> None:
    v = judge(
        _flow(), _seen(_found("submit", 0), _found("enrich", 5), _found("settle", 20)), now=T0
    )
    assert (v.kind, v.deviation) == ("complete", None)


def test_stuck_past_its_time_is_the_deviation() -> None:
    seen = _seen(_found("submit", 0), _found("enrich", 5), StageObservation("settle", "missing"))
    v = judge(_flow(), seen, now=T0 + timedelta(hours=2))
    assert (v.kind, v.stage, v.since) == ("stuck", "settle", T0 + timedelta(minutes=5))
    assert v.deviation == "settle"


def test_waiting_within_its_time_is_in_progress() -> None:
    seen = _seen(_found("submit", 0), _found("enrich", 5), StageObservation("settle", "missing"))
    v = judge(_flow(), seen, now=T0 + timedelta(minutes=10))
    assert (v.kind, v.stage, v.deviation) == ("in_progress", "settle", None)


def test_rejected_on_purpose() -> None:
    seen = _seen(_found("submit", 0), _found("enrich", 5, rejected=True))
    assert judge(_flow(), seen, now=T0).kind == "rejected"


def test_late_stage_is_listed_even_when_complete() -> None:
    seen = _seen(_found("submit", 0), _found("enrich", 50), _found("settle", 60))
    v = judge(_flow(), seen, now=T0)
    assert (v.kind, v.late, v.deviation) == ("complete", ("enrich",), "enrich")


def test_unknown_deciding_stage_cannot_tell() -> None:
    seen = _seen(_found("submit", 0), StageObservation("enrich", "unknown", reason="tool error"))
    v = judge(_flow(), seen, now=T0 + timedelta(hours=2))
    assert (v.kind, v.stage, v.reason) == ("cannot_tell", "enrich", "tool error")


def test_a_passed_stage_with_no_trace_is_a_correlation_gap() -> None:
    seen = _seen(_found("submit", 0), StageObservation("enrich", "missing"), _found("settle", 20))
    v = judge(_flow(), seen, now=T0)
    assert (v.kind, v.uncorrelated) == ("complete", ("enrich",))


def test_never_seen_is_not_found() -> None:
    seen = _seen(StageObservation("submit", "missing"))
    assert judge(_flow(), seen, now=T0).kind == "not_found"


def test_no_time_limit_never_reads_as_stuck() -> None:
    flow = _flow(stages=[_stage("submit"), _stage("settle", within=None, terminal=True)])
    seen = _seen(_found("submit", 0), StageObservation("settle", "missing"))
    v = judge(flow, seen, now=T0 + timedelta(days=3))
    assert (v.kind, v.reason) == ("in_progress", "no time limit declared")
