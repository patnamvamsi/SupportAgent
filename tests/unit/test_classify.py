"""WP-010: ``Classifier.decide`` — the ARCH §4 branch order, one test per branch.

Canonical test IDs from 10-wp §WP-010 (named after the ARCH reason codes):
``test_decide_llm_none_safe_default``, ``test_decide_conflict_safe_default``,
``test_decide_low_score_safe_default``,
``test_decide_llm_only_p1_capped_with_suggestion``,
``test_decide_abstain_p3_routes``, ``test_decide_unresolved_service_safe_default``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from supportagent.app.classify import Classifier
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.case import Case
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.triage import TriageResult
from supportagent.domain.preclass import PreclassRule, RuleSet
from supportagent.domain.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.llm import StructuredLLM
from tests.builders import an_alert

_MODEL = "claude-sonnet-4-5-20250929"
_LABEL = "payments/api"
# n=40, all correct: wilson_lower_bound(40, 40) ≈ 0.912 >= 0.80 (clears the floor).
_STRONG = CalibrationCountRow(n=40, correct=40)
# n=10, all correct: wilson_lower_bound(10, 10) ≈ 0.723 < 0.80 (below the floor).
_WEAK = CalibrationCountRow(n=10, correct=10)
TRACE = TraceContext.new("acme", "trace-1", "span-1")


class _FakeLLM:
    """A ``StructuredLLM`` that returns a canned result (or ``None``)."""

    def __init__(self, result: TriageResult | None) -> None:
        self._result = result

    def structured(
        self,
        slot: SlotName,
        variables: Mapping[str, str],
        schema: type[Any],
        trace: TraceContext,
    ) -> Any:
        # The classifier must call the ``classify`` slot with the prompt vars.
        assert slot is SlotName.CLASSIFY
        assert set(variables) == {"ticket_subject", "ticket_body"}
        return self._result


def _case(**overrides: object) -> Case:
    base: dict[str, Any] = {
        "id": "case-1",
        "tenant_id": "acme",
        "fingerprint": "fp-1",
        "origin": "alert",
        "service": "payroll-api",
    }
    base.update(overrides)
    return Case.model_validate(base)


def _evt(**overrides: object) -> AlertEvent:
    base = an_alert()
    base.update(overrides)
    return AlertEvent.model_validate(base)


def _table(label: str, agree: CalibrationCountRow | None, abstain: CalibrationCountRow | None) -> CalibrationTable:
    return CalibrationTable(
        grade="smoke",
        snapshot_id="snap-1",
        model_id=_MODEL,
        labels={label: CalibrationLabelRow(agree=agree, abstain=abstain)},
    )


def _routing() -> RoutingTable:
    return RoutingTable(
        rules=[
            RoutingRule(
                match_classification=_LABEL,
                severities=[Severity.P1, Severity.P2, Severity.P3, Severity.P4],
                route=Route(id="payments-route", actions=[ActionKind.WORK_NOTE]),
            )
        ],
        default=Route(id="generic", actions=[ActionKind.WORK_NOTE]),
    )


def _classifier(
    *,
    llm_result: TriageResult | None,
    rule_set: RuleSet | None = None,
    table: CalibrationTable | None = None,
) -> Classifier:
    return Classifier(
        rule_set=rule_set if rule_set is not None else RuleSet(rules=[]),
        llm=_FakeLLM(llm_result),
        calibration=table if table is not None else _table(_LABEL, _STRONG, _STRONG),
        min_score=0.80,
        routing=_routing(),
    )


def test_decide_llm_none_safe_default() -> None:
    # ARCH §4: ``llm.classify`` returns None (no usable answer) -> safe default.
    d = _classifier(llm_result=None).decide(_evt(), _case(), TRACE)
    assert d.outcome == "safe_default"
    assert d.reason == "llm-failure"
    assert d.llm_suggestion is None


def test_decide_conflict_safe_default() -> None:
    # ARCH §4: a pre-classifier label in a different domain than the LLM's
    # classification is a conflict -> safe default (never trust either blindly).
    llm = TriageResult(classification=_LABEL, severity=Severity.P3, summary="s", confidence=0.9)
    rule_set = RuleSet(rules=[PreclassRule(when={"subject": "dns"}, label="network/dns")])
    d = _classifier(llm_result=llm, rule_set=rule_set).decide(
        _evt(subject="dns resolution timeout"), _case(), TRACE
    )
    assert d.outcome == "safe_default"
    assert d.reason == "classifier-conflict"
    assert d.preclass_label == "network/dns"


def test_decide_low_score_safe_default() -> None:
    # ARCH §4: calibrated_score below calibration.min_score -> safe default.
    llm = TriageResult(classification=_LABEL, severity=Severity.P3, summary="s", confidence=0.9)
    table = _table(_LABEL, _WEAK, _WEAK)
    d = _classifier(llm_result=llm, table=table).decide(_evt(), _case(), TRACE)
    assert d.outcome == "safe_default"
    assert d.reason == "low-calibrated-score"
    assert d.calibrated_score is not None and d.calibrated_score < 0.80


def test_decide_llm_only_p1_capped_with_suggestion() -> None:
    # ARCH §4: no pre-classifier label and the LLM asserts P1 -> safe default
    # (unconfirmed high severity) carrying the LLM result as a suggestion.
    llm = TriageResult(classification=_LABEL, severity=Severity.P1, summary="s", confidence=0.9)
    table = _table(_LABEL, _STRONG, _STRONG)
    d = _classifier(llm_result=llm, table=table).decide(_evt(), _case(), TRACE)
    assert d.outcome == "safe_default"
    assert d.reason == "unconfirmed-high-severity"
    assert d.llm_suggestion is not None
    assert d.llm_suggestion.classification == _LABEL
    assert d.llm_suggestion.severity == Severity.P1


def test_decide_abstain_p3_routes() -> None:
    # ARCH §4: no pre-classifier label, LLM says P3, and the *abstain* row
    # clears the floor -> routed (the abstain row, not the agree row, scores).
    llm = TriageResult(classification=_LABEL, severity=Severity.P3, summary="s", confidence=0.9)
    # Agree row is weak (would fail the floor); abstain row is strong (passes).
    table = _table(_LABEL, _WEAK, _STRONG)
    d = _classifier(llm_result=llm, table=table).decide(_evt(), _case(), TRACE)
    assert d.outcome == "routed"
    assert d.reason == "routed"
    assert d.route_id == "payments-route"
    assert d.llm_suggestion is None


def test_decide_unresolved_service_safe_default() -> None:
    # ARCH §4: the catalog could not resolve the case's service -> safe default,
    # checked first (before the LLM is even consulted).
    llm = TriageResult(classification=_LABEL, severity=Severity.P3, summary="s", confidence=0.9)
    d = _classifier(llm_result=llm).decide(_evt(), _case(service=None), TRACE)
    assert d.outcome == "safe_default"
    assert d.reason == "unresolved-service"
