"""ADR-0021 §6: the trace lab's scoring and its pipeline.

A wrong stopping stage is the trace's false cause; "can't tell" is the safe
default and never counts as one. A seeded PII value in any model request
fails the leak gate. The pipeline runs the real tracer and explainer over a
scenario's logs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from supportagent.bootstrap import TraceLabPipeline, load_flow_file, load_trace_suites
from supportagent.evals.trace_lab import (
    TraceGates,
    TraceOutcome,
    TraceScenario,
    TraceSuite,
    TraceTruth,
    run_trace_lab,
)
from supportagent.ports.llm import ProviderRequest, ProviderResponse
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

FLOW = Path("evals/trace/system-c/flow.yaml")


def _outcome(
    kind: str, stage: str | None, *, requests: tuple[str, ...] = (), kept: int = 1
) -> TraceOutcome:
    return TraceOutcome(kind, stage, (), "why" if kind == "cannot_tell" else "", requests, kept, 0)


def _scenario(
    sid: str, kind: str, stage: str | None, pii: list[str] | None = None
) -> TraceScenario:
    return TraceScenario.model_validate(
        {
            "id": sid,
            "object_id": "SHP-000001",
            "now": "2026-11-02T10:00:00Z",
            "pii": [{"value": v, "entity": "PERSON"} for v in (pii or [])],
            "truth": {"kind": kind, "stage": stage},
        }
    )


def _report(pairs: list[tuple[TraceScenario, TraceOutcome]], **gates: Any) -> Any:
    got = {s.id: o for s, o in pairs}
    suite = TraceSuite(flow="f", scenarios=[s for s, _ in pairs])
    return run_trace_lab(
        suite,
        lambda s: got[s.id],
        gates=TraceGates(min_cases=1, **gates),
        mode="replay",
        model_id="m",
    )


def test_wrong_stage_cases() -> None:
    report = _report(
        [
            (_scenario("stuck-right", "stuck", "b"), _outcome("stuck", "b")),
            (_scenario("stuck-wrong", "stuck", "b"), _outcome("stuck", "c")),
            (_scenario("missed-stop", "stuck", "b"), _outcome("in_progress", "b")),
            (_scenario("invented-stop", "complete", None), _outcome("stuck", "b")),
            (_scenario("safe-default", "stuck", "b"), _outcome("cannot_tell", "b")),
        ]
    )
    wrong = {s.scenario_id for s in report.scores if s.wrong_stage}
    assert wrong == {"stuck-wrong", "missed-stop", "invented-stop"}
    gate = {g.name: g for g in report.gates}
    assert not gate["wrong_stage"].ok
    assert gate["deviation_found"].detail.startswith("1/4")


def test_seeded_pii_in_a_request_fails_the_leak_gate() -> None:
    report = _report(
        [
            (
                _scenario("leaky", "complete", None, pii=["Jane Doe"]),
                _outcome("complete", None, requests=("facts: Jane Doe shipped",)),
            ),
            (
                _scenario("clean", "complete", None, pii=["Jane Doe"]),
                _outcome("complete", None, requests=("facts: <PERSON_1> shipped",)),
            ),
        ]
    )
    gate = {g.name: g for g in report.gates}
    assert not gate["pii_leaks"].ok and "leaky" in gate["pii_leaks"].detail
    assert [s.ok for s in report.scores] == [False, True]


def test_cannot_tell_must_name_its_stage() -> None:
    unnamed = TraceOutcome("cannot_tell", None, (), "", (), 0, 0)
    report = _report([(_scenario("x-unnamed", "cannot_tell", "a"), unnamed)])
    assert not {g.name: g for g in report.gates}["cannot_tell_names_stage"].ok


def test_answers_kept_gate() -> None:
    report = _report(
        [(_scenario("one", "complete", None), _outcome("complete", None, kept=0))],
        min_answers_kept=0.5,
    )
    assert not {g.name: g for g in report.gates}["answers_kept"].ok


def test_truth_validates() -> None:
    with pytest.raises(ValueError, match="kind"):
        TraceTruth.model_validate({"kind": "lost"})


class _Echo:
    """Answers explain_trace citing the verdict, and supports every check."""

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if "explain where one business object is" in request.system:
            body = '{"engineer": [], "business": [{"text": "It is on its way.", "cites": ["V"]}]}'
        else:
            body = '{"verdict": "supports"}'
        return ProviderResponse(body, "fake", 1, 1, 1)


def test_pipeline_runs_the_fixture_without_leaking() -> None:
    suites, gates = load_trace_suites(
        _shadow_config().model_copy(
            update={
                "paths": _shadow_config().paths.model_copy(update={"gates": "evals/gates.yaml"})
            }
        )
    )
    flow, suite = suites[0]
    assert flow.flow == "shipment-tracking" and gates.max_pii_leaks == 0
    pipeline = TraceLabPipeline(_shadow_config(), load_flow_file(FLOW), provider=_Echo())
    report = run_trace_lab(suite, pipeline.run, gates=gates, mode="replay", model_id="fake")
    assert report.passed, [g for g in report.gates if not g.ok]
    assert all(s.got.business_kept == 1 for s in report.scores)
    assert all(s.got.requests for s in report.scores)  # every answer went through the model
