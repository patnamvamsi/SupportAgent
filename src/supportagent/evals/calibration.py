"""``--write-calibration`` (ADR-0002, 01 §7.7): per-label counts from a **live** smoke run.

``calibrated_score`` looks up the label the LLM *predicted*, split by whether
the pre-classifier agreed. So the counts are keyed the same way: for every
case with a truth label and an LLM answer, ``n`` counts the prediction under
its label's ``agree`` row (the pre-classifier's label matched it) or
``abstain`` row (the pre-classifier abstained), and ``correct`` counts the
predictions that prefix-match the truth. Conflicts (the pre-classifier named
another domain) never route, so they aren't calibration evidence. Neither
are LLM failures, which have no prediction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal

from supportagent.domain.classification import prefix_match
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.quality import CaseOutcome
from supportagent.evals.cases import EvalCase
from supportagent.evals.scoring import CaseRun


def build_calibration(
    pairs: Sequence[tuple[EvalCase, CaseRun]],
    *,
    model_id: str,
    snapshot_id: str,
    report_id: str | None = None,
) -> CalibrationTable:
    counts: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"agree": [0, 0], "abstain": [0, 0]}
    )
    for case, run in pairs:
        truth = case.expect.classification
        predicted = run.decision.classification
        if truth is None or predicted is None or run.linked:
            continue
        preclass = run.decision.preclass_label
        if preclass is None:
            row = "abstain"
        elif prefix_match(preclass, predicted):
            row = "agree"
        else:
            continue  # classifier-conflict: never routes, not evidence
        cell = counts[predicted][row]
        cell[0] += 1
        cell[1] += int(prefix_match(truth, predicted))
    table = _table(counts, grade="smoke", model_id=model_id, snapshot_id=snapshot_id)
    return table.signed(report_id) if report_id else table


def build_holdout_calibration(
    outcomes: Sequence[CaseOutcome],
    preclass: Mapping[str, str | None],
    *,
    model_id: str,
    snapshot_id: str,
    report_id: str,
) -> CalibrationTable:
    """WP-105: ``grade: holdout`` counts from the quality suite, signed with its report id.

    One observation per **case** (its majority prediction), keyed the same way
    as the smoke table: the predicted label's ``agree`` row (the
    pre-classifier's label matched it) or ``abstain`` row (it abstained).
    """
    counts: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"agree": [0, 0], "abstain": [0, 0]}
    )
    for outcome in outcomes:
        predicted = outcome.classification
        if predicted is None:
            continue
        label = preclass.get(outcome.case_id)
        if label is None:
            row = "abstain"
        elif prefix_match(label, predicted):
            row = "agree"
        else:
            continue  # conflict: never routes, not evidence
        cell = counts[predicted][row]
        cell[0] += 1
        cell[1] += int(outcome.classification_correct)
    table = _table(counts, grade="holdout", model_id=model_id, snapshot_id=snapshot_id)
    return table.signed(report_id)


def _table(
    counts: Mapping[str, Mapping[str, list[int]]],
    *,
    grade: Literal["smoke", "holdout"],
    model_id: str,
    snapshot_id: str,
) -> CalibrationTable:
    labels = {
        label: CalibrationLabelRow(
            agree=CalibrationCountRow(n=rows["agree"][0], correct=rows["agree"][1])
            if rows["agree"][0]
            else None,
            abstain=CalibrationCountRow(n=rows["abstain"][0], correct=rows["abstain"][1])
            if rows["abstain"][0]
            else None,
        )
        for label, rows in sorted(counts.items())
    }
    return CalibrationTable(
        version=1, grade=grade, snapshot_id=snapshot_id, model_id=model_id, labels=labels
    )


__all__ = ["build_calibration", "build_holdout_calibration"]
