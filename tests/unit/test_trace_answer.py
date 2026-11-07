"""ADR-0021 §5: a traced object's answer, cited and checked.

The model writes only over numbered facts from the trace. A sentence that
cites an unknown fact is dropped; one the check doesn't find supported is
dropped too. The template answer needs no model and always stands. RCA runs
only where the object stopped on its own, and its causes are pseudonymised.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from supportagent.app.rca import Budget, RcaArtifact, RcaCase
from supportagent.app.trace_answer import TraceExplainer
from supportagent.app.tracing import TraceRun
from supportagent.domain.flow_answer import facts, template_answer
from supportagent.domain.flow_trace import StageObservation, Verdict
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.rca_reduce import Claim
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from tests.unit.test_flows import _flow
from tests.unit.test_rca import ScriptedLLM

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
TRACE = TraceContext.new("acme", "tr_answer", "span_answer")
CATALOG = Catalog(version="1", services=[ServiceEntry(name="settle")])


def _stuck_run(**over: Any) -> TraceRun:
    obs = (
        StageObservation("submit", "found", T0),
        StageObservation("enrich", "found", T0 + timedelta(minutes=5)),
        StageObservation("settle", "missing"),
    )
    values: dict[str, Any] = {
        "flow": "trade-settlement",
        "object_id": "T-1234",
        "observations": obs,
        "verdict": Verdict("stuck", "settle", T0 + timedelta(minutes=5)),
        "evidence": {"enrich": {"matches": [{"ts": "x", "line": "enriched for <PERSON_1>"}]}},
        "pseudonymise": lambda t: t.replace("Jane Doe", "<PERSON_1>"),
    }
    values.update(over)
    return TraceRun(**values)


def _explanation(
    business: list[dict[str, Any]], engineer: list[dict[str, Any]] | None = None
) -> Any:
    return lambda _v: {"engineer": engineer or [], "business": business}


def test_facts_are_numbered_and_carry_no_raw_values() -> None:
    run = _stuck_run()
    got = facts(_flow(), run.object_id, run.observations, run.verdict, run.evidence, ["C text"])
    ids = [f.id for f in got]
    assert ids[:4] == ["S1", "S2", "S3", "V"]
    assert "E1" in ids and ids[-1] == "C1"
    assert "stuck before 'Settle'" in next(f.text for f in got if f.id == "V")
    assert any("<PERSON_1>" in f.text for f in got)


def test_template_answer_needs_no_model() -> None:
    answer = TraceExplainer(llm=None, catalog=CATALOG).explain(
        _flow(), _stuck_run(), TRACE, pseudonymise=None
    )
    assert answer.summary == answer.template
    assert answer.template.startswith(
        "Trade T-1234 is stuck before 'Settle' since 2026-11-08T09:05"
    )


def test_sentences_with_unknown_cites_or_unsupported_are_dropped() -> None:
    checks: list[str] = []

    def check(v: Mapping[str, str]) -> object:
        checks.append(v["sentence"])
        return {"verdict": "unknown" if "because" in v["sentence"] else "supports"}

    llm = ScriptedLLM(
        {
            SlotName.EXPLAIN_TRACE: _explanation(
                [
                    {"text": "Your trade was booked but hasn't settled yet.", "cites": ["V"]},
                    {"text": "It failed because of a holiday.", "cites": ["V"]},
                    {"text": "It was cancelled.", "cites": ["X9"]},
                ]
            ),
            SlotName.CHECK_TRACE: check,
        }
    )
    answer = TraceExplainer(llm=llm, catalog=CATALOG).explain(
        _flow(), _stuck_run(), TRACE, pseudonymise=None
    )
    assert [s.text for s in answer.business] == ["Your trade was booked but hasn't settled yet."]
    assert answer.dropped == 2
    assert len(checks) == 2  # the bad-cite sentence never reaches the check
    assert answer.summary == "Your trade was booked but hasn't settled yet."


def test_no_surviving_business_sentence_falls_back_to_the_template() -> None:
    llm = ScriptedLLM(
        {
            SlotName.EXPLAIN_TRACE: _explanation([{"text": "Guess.", "cites": ["V"]}]),
            SlotName.CHECK_TRACE: lambda _v: {"verdict": "contradicts"},
        }
    )
    answer = TraceExplainer(llm=llm, catalog=CATALOG).explain(
        _flow(), _stuck_run(), TRACE, pseudonymise=None
    )
    assert answer.summary == template_answer(_flow(), "T-1234", _stuck_run().verdict)


def test_model_failure_keeps_the_template() -> None:
    answer = TraceExplainer(llm=ScriptedLLM({}), catalog=CATALOG).explain(
        _flow(), _stuck_run(), TRACE, pseudonymise=None
    )
    assert answer.summary == answer.template and answer.engineer == ()


class _Rca:
    def __init__(self) -> None:
        self.cases: list[RcaCase] = []

    def __call__(self, case: RcaCase, budget: Budget) -> RcaArtifact:
        self.cases.append(case)
        return RcaArtifact(
            "SUPPORTED",
            "check held",
            "proposed",
            claims=[
                Claim("consumer_lag: lag 9000 for Jane Doe's account", ["s1"]),
                Claim("a guess", ["s2"], tags={"speculation"}),
            ],
            supported_by="s1",
        )


def test_rca_runs_on_the_stopping_component_and_its_causes_are_pseudonymised() -> None:
    rca = _Rca()
    flow = _flow()
    answer = TraceExplainer(llm=None, catalog=CATALOG, rca=rca).explain(
        flow, _stuck_run(), TRACE, pseudonymise=_stuck_run().pseudonymise
    )
    assert rca.cases[0].service is not None and rca.cases[0].service.name == "settle"
    assert rca.cases[0].opened_ts == int((T0 + timedelta(minutes=5)).timestamp())
    causes = [f.text for f in answer.facts if f.id.startswith("C")]
    assert causes == ["consumer_lag: lag 9000 for <PERSON_1>'s account"]
    assert answer.rca["outcome"] == "SUPPORTED"


def test_rca_causes_are_withheld_without_pii() -> None:
    rca = _Rca()
    answer = TraceExplainer(llm=None, catalog=CATALOG, rca=rca).explain(
        _flow(), _stuck_run(pseudonymise=None), TRACE, pseudonymise=None
    )
    assert not [f for f in answer.facts if f.id.startswith("C")]
    assert "causes_withheld" in answer.rca


def test_rca_skips_waiting_and_non_catalog_components() -> None:
    rca = _Rca()
    waiting = _stuck_run(verdict=Verdict("in_progress", "settle", T0))
    TraceExplainer(llm=None, catalog=CATALOG, rca=rca).explain(
        _flow(), waiting, TRACE, pseudonymise=None
    )
    assert rca.cases == []
    other = TraceExplainer(
        llm=None, catalog=Catalog(version="1", services=[ServiceEntry(name="zz")]), rca=rca
    )
    answer = other.explain(_flow(), _stuck_run(), TRACE, pseudonymise=None)
    assert rca.cases == [] and "isn't a catalog service" in str(answer.rca["skipped"])
