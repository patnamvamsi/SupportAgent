"""The classifier: ARCH §4 branch order, one safe default per reason.

``Classifier.decide`` applies the decision branches in exactly the ARCH §4
pseudo-code order, each safe default named after its ARCH reason code:

1. ``unresolved-service``  — the catalog couldn't resolve the case's service.
   (``no-paging-integration`` — the service has no paging integration — is
   decided by ``TriageService`` just after, before the classifier runs:
   ARCH §14, ADR-0006.)
2. ``llm-failure``         — the structured LLM returned no usable answer.
3. ``classifier-conflict`` — pre-classifier and LLM disagree on the domain.
4. ``low-calibrated-score``— the calibrated score is below the configured floor.
5. ``unconfirmed-high-severity`` — no pre-classifier label and the LLM asserts P1/P2.
6. ``routed``              — the routing table's first match (or the default).

It is pure application code: no adapters, no I/O. The LLM is the injected
``StructuredLLM`` port and the tables (rules, calibration, routing) are domain
values; bootstrap wires the loaders (WP-011).
"""

from __future__ import annotations

from typing import Literal

from supportagent.domain.classification import calibrated_score, prefix_match
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.model.case import Case
from supportagent.domain.model.triage import TriageDecision, TriageResult
from supportagent.domain.preclass import RuleSet
from supportagent.domain.routing import RoutingTable
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.llm import StructuredLLM

_DecideReason = Literal[
    "llm-failure",
    "classifier-conflict",
    "low-calibrated-score",
    "unconfirmed-high-severity",
    "unresolved-service",
    "no-paging-integration",
    "storm-member",
    "spend-breaker",
    "routed",
]


class Classifier:
    """Decides how a case is triaged (ARCH §4). Stateless; all inputs injected."""

    def __init__(
        self,
        *,
        rule_set: RuleSet,
        llm: StructuredLLM,
        calibration: CalibrationTable,
        min_score: float,
        routing: RoutingTable,
    ) -> None:
        self.rule_set = rule_set
        self.llm = llm
        self.calibration = calibration
        self.min_score = min_score
        self.routing = routing

    def decide(self, evt: AlertEvent, case: Case, trace: TraceContext) -> TriageDecision:
        # 1. unresolved-service: checked first, before the LLM is consulted.
        if case.service is None:
            return safe_default("unresolved-service")

        label = self.rule_set.classify(evt)

        # 2. llm-failure: the structured LLM returned no usable answer.
        llmres = self.llm.structured(
            SlotName.CLASSIFY,
            {"ticket_subject": evt.subject, "ticket_body": evt.body},
            TriageResult,
            trace,
        )
        if llmres is None:
            # The pre-classifier ran: keep its label so abstain/conflict rates
            # (ARCH §4.2) are not skewed by LLM failures.
            return safe_default("llm-failure", preclass_label=label)

        # 3. classifier-conflict: the pre-classifier and the LLM disagree on domain.
        if label is not None and not prefix_match(label, llmres.classification):
            return safe_default(
                "classifier-conflict",
                classification=llmres.classification,
                severity=llmres.severity,
                preclass_label=label,
            )

        # 4. low-calibrated-score: below the configured calibration floor.
        agrees = label is not None and prefix_match(label, llmres.classification)
        score = calibrated_score(self.calibration, llmres.classification, agrees)
        if score < self.min_score:
            return safe_default(
                "low-calibrated-score",
                classification=llmres.classification,
                severity=llmres.severity,
                preclass_label=label,
                calibrated_score=score,
            )

        # 5. unconfirmed-high-severity: LLM asserts P1/P2 without a pre-classifier
        #    label — cap it to a safe default carrying the LLM result as a suggestion.
        if label is None and llmres.severity in (Severity.P1, Severity.P2):
            return safe_default(
                "unconfirmed-high-severity",
                classification=llmres.classification,
                severity=llmres.severity,
                calibrated_score=score,
                llm_suggestion=llmres,
            )

        # 6. routed: first matching routing rule, else the default (generic).
        route = self.routing.lookup(llmres.classification, llmres.severity)
        return TriageDecision(
            outcome="routed",
            reason="routed",
            classification=llmres.classification,
            severity=llmres.severity,
            preclass_label=label,
            calibrated_score=score,
            route_id=route.id,
            llm_suggestion=None,
        )


def safe_default(  # noqa: PLR0913 — one optional field per ARCH §4 branch
    reason: _DecideReason,
    *,
    classification: str | None = None,
    severity: Severity | None = None,
    preclass_label: str | None = None,
    calibrated_score: float | None = None,
    llm_suggestion: TriageResult | None = None,
) -> TriageDecision:
    return TriageDecision(
        outcome="safe_default",
        reason=reason,
        classification=classification,
        severity=severity,
        preclass_label=preclass_label,
        calibrated_score=calibrated_score,
        route_id=None,
        llm_suggestion=llm_suggestion,
    )
