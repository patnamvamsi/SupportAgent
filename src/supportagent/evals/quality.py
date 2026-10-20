"""The quality suite (ARCH §11, 04 §8; 11-wp WP-104).

``run_quality`` classifies every holdout case ``samples`` times through a
``QualitySampler`` (bootstrap: the configured classifier behind the
response cache, or the fixture LLM for wiring). It then scores the
**per-case majority** with ``domain.quality``: Wilson lower bounds over
cases per severity band, the severity cost total, the gates, a per-case diff
against the baseline report, and detector recall on the held-out secret
corpus.

``write_quality_report`` writes ``report.json`` and ``summary.md`` (the
human-readable gate table and per-case diff) under ``evals/artifacts/<run>/``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from supportagent.domain.quality import (
    DEFAULT_GATES,
    CaseDiff,
    CaseOutcome,
    GateResult,
    QualityGate,
    band_scores,
    cost_total,
    diff_against_baseline,
    evaluate_gates,
)
from supportagent.evals.cases import EvalCase


class QualitySampler(Protocol):
    mode: str
    model_id: str

    def sample(self, case: EvalCase, index: int, sample: int) -> tuple[str | None, str | None]:
        """One classification of ``case``: ``(classification, severity)``."""
        ...


@dataclass(frozen=True, slots=True)
class RecallReport:
    total: int
    detected: int
    by_class: Mapping[str, tuple[int, int]]  # class -> (detected, total)

    @property
    def recall(self) -> float | None:
        return self.detected / self.total if self.total else None

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "detected": self.detected,
            "recall": self.recall,
            "by_class": {
                k: {"detected": d, "total": t} for k, (d, t) in sorted(self.by_class.items())
            },
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    mode: str
    model_id: str
    snapshot_id: str
    samples: int
    outcomes: list[CaseOutcome]
    gates: list[GateResult]
    diff: CaseDiff | None
    recall: RecallReport | None
    skipped: list[str]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def per_case(self) -> dict[str, bool]:
        return {o.case_id: o.classification_correct and o.severity_correct for o in self.outcomes}

    def as_dict(self) -> dict[str, object]:
        scores = band_scores(self.outcomes)
        return {
            "suite": "quality",
            "mode": self.mode,
            "model_id": self.model_id,
            "snapshot_id": self.snapshot_id,
            "samples": self.samples,
            "passed": self.passed,
            "cases": len(self.outcomes),
            "bands": {k: v.as_dict() for k, v in scores.items()},
            "cost_total": cost_total(self.outcomes),
            "gates": [_gate_dict(g) for g in self.gates],
            "diff": None if self.diff is None else _diff_dict(self.diff),
            "detector_recall": None if self.recall is None else self.recall.as_dict(),
            "skipped": self.skipped,
            "per_case": [
                {
                    "case": o.case_id,
                    "truth": [o.truth_classification, o.truth_severity],
                    "majority": [o.classification, o.severity],
                    "samples": [list(s) for s in o.samples],
                    "passed": o.classification_correct and o.severity_correct,
                    "cost": o.cost,
                }
                for o in self.outcomes
            ],
        }


def _gate_dict(g: GateResult) -> dict[str, object]:
    return {"name": g.name, "passed": g.passed, "value": g.value, "reason": g.reason}


def _diff_dict(d: CaseDiff) -> dict[str, list[str]]:
    return {
        "newly_failing": d.newly_failing,
        "newly_passing": d.newly_passing,
        "added": d.added,
        "removed": d.removed,
    }


def run_quality(  # noqa: PLR0913 — the suite's inputs
    cases: Sequence[EvalCase],
    sampler: QualitySampler,
    *,
    samples: int,
    snapshot_id: str,
    gates: Sequence[QualityGate] = DEFAULT_GATES,
    baseline: Mapping[str, bool] | None = None,
    recall: RecallReport | None = None,
) -> QualityReport:
    if samples < 1:
        raise ValueError("samples must be >= 1")  # noqa: TRY003
    outcomes: list[CaseOutcome] = []
    skipped: list[str] = []
    for index, case in enumerate(cases):
        truth_c, truth_s = case.expect.classification, case.expect.severity
        if sampler.mode not in case.modes or truth_c is None or truth_s is None:
            skipped.append(case.id)
            continue
        outcomes.append(
            CaseOutcome(
                case_id=case.id,
                truth_classification=truth_c,
                truth_severity=truth_s,
                samples=[sampler.sample(case, index, s) for s in range(samples)],
            )
        )
    report = QualityReport(
        mode=sampler.mode,
        model_id=sampler.model_id,
        snapshot_id=snapshot_id,
        samples=samples,
        outcomes=outcomes,
        gates=evaluate_gates(band_scores(outcomes), gates),
        diff=None,
        recall=recall,
        skipped=skipped,
    )
    if baseline is not None:
        report = replace(report, diff=diff_against_baseline(report.per_case(), baseline))
    return report


def baseline_from(path: Path) -> dict[str, bool]:
    """``case → passed`` from an earlier quality ``report.json``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(row["case"]): bool(row["passed"]) for row in data.get("per_case", [])}


def write_quality_report(artifacts: Path, report: QualityReport, *, now: datetime) -> Path:
    run_id = f"{now:%Y%m%dT%H%M%SZ}-quality-{report.mode}"
    out = artifacts / run_id
    out.mkdir(parents=True, exist_ok=True)
    body = report.as_dict()
    (out / "report.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(_summary(body), encoding="utf-8")
    return out / "report.json"


def _summary(body: Mapping[str, object]) -> str:
    lines = [
        f"# Quality suite: {'PASS' if body['passed'] else 'FAIL'}",
        "",
        f"Model `{body['model_id']}`, snapshot `{body['snapshot_id']}`, {body['samples']} "
        f"sample(s) per case, {body['cases']} case(s). Thresholds are provisional (ADR-0003).",
        "",
        "| Gate | Result | Value | Why |",
        "|---|---|---|---|",
    ]
    for g in body["gates"]:  # type: ignore[attr-defined]
        lines.append(
            f"| {g['name']} | {'pass' if g['passed'] else 'FAIL'} | {g['value']} | {g['reason']} |"
        )
    lines += ["", "| Band | Cases | Classification LB | Severity LB |", "|---|---|---|---|"]
    for band in body["bands"].values():  # type: ignore[attr-defined]
        lines.append(
            f"| {band['band']} | {band['cases']} | {band['classification_lb']:.3f} | "
            f"{band['severity_lb']:.3f} |"
        )
    lines += ["", f"Severity cost total: {body['cost_total']}."]
    recall = body.get("detector_recall")
    if isinstance(recall, dict) and recall.get("recall") is not None:
        lines.append(f"Detector recall (held-out corpus): {recall['recall']:.2%}.")
    diff = body.get("diff")
    if isinstance(diff, dict):
        lines += ["", "## Per-case diff against the baseline", ""]
        for key in ("newly_failing", "newly_passing", "added", "removed"):
            items = diff.get(key) or []
            lines.append(f"- **{key.replace('_', ' ')}** ({len(items)}): {', '.join(items) or '-'}")
    return "\n".join(lines) + "\n"


__all__ = [
    "QualityReport",
    "QualitySampler",
    "RecallReport",
    "baseline_from",
    "run_quality",
    "write_quality_report",
]
