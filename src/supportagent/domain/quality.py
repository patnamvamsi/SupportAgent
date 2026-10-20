"""Quality-suite scoring (ARCH §11; 01 §7.6, §7.9; 11-wp WP-104).

- **Per-case majority.** A case's N samples are one observation. Its label is
  the majority classification (prefix-matched to the truth), and its severity
  the majority severity. Ties go to the earliest sample, so the result is
  deterministic.
- **Wilson lower bound over cases, not cases x N:** the N samples of one case
  aren't independent observations.
- **Per severity band** of the *truth* (P1-P2, P3-P4): classification correctness
  and severity correctness.
- **Severity cost matrix** (01 §7.9): misgrading a P1 as P3 costs far more
  than a P3 as P1.
- **Gates** (``evals/gates.yaml`` ``quality``, prov.): each is a lower bound
  per band. The report says why each check passed or failed.
- **Baseline diff:** per case, which cases newly fail and which newly pass.
- **Split rule** (ROADMAP §2.1, ARCH §10.1): every training case is earlier
  than every holdout case, and no normalized signature is shared.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from supportagent.domain.classification import prefix_match
from supportagent.domain.stats import wilson_lower_bound

BANDS: Mapping[str, frozenset[str]] = {
    "P1-P2": frozenset({"P1", "P2"}),
    "P3-P4": frozenset({"P3", "P4"}),
}
#: 01 §7.9: rows are truth, columns are prediction.
COST: Mapping[str, Mapping[str, int]] = {
    "P1": {"P1": 0, "P2": 3, "P3": 8, "P4": 10},
    "P2": {"P1": 1, "P2": 0, "P3": 3, "P4": 6},
    "P3": {"P1": 1, "P2": 1, "P3": 0, "P4": 2},
    "P4": {"P1": 2, "P2": 1, "P3": 1, "P4": 0},
}
#: A missing prediction (the model failed every sample) costs the worst cell of its row.
_MISSING = "none"


def majority[T](values: Sequence[T | None]) -> T | None:
    """The most common non-``None`` value; ties go to the earliest sample."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    counts = Counter(present)
    top = max(counts.values())
    return next(v for v in present if counts[v] == top)


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case_id: str
    truth_classification: str
    truth_severity: str
    samples: Sequence[tuple[str | None, str | None]]  # (classification, severity) per sample

    @property
    def classification(self) -> str | None:
        return majority([c for c, _ in self.samples])

    @property
    def severity(self) -> str | None:
        return majority([s for _, s in self.samples])

    @property
    def classification_correct(self) -> bool:
        got = self.classification
        return got is not None and prefix_match(self.truth_classification, got)

    @property
    def severity_correct(self) -> bool:
        return self.severity == self.truth_severity

    @property
    def cost(self) -> int:
        row = COST[self.truth_severity]
        return row.get(self.severity or _MISSING, max(row.values()))

    @property
    def band(self) -> str:
        return next(b for b, members in BANDS.items() if self.truth_severity in members)


@dataclass(frozen=True, slots=True)
class BandScore:
    band: str
    cases: int
    classification_correct: int
    severity_correct: int

    @property
    def classification_lb(self) -> float:
        return wilson_lower_bound(self.classification_correct, self.cases)

    @property
    def severity_lb(self) -> float:
        return wilson_lower_bound(self.severity_correct, self.cases)

    def as_dict(self) -> dict[str, object]:
        return {
            "band": self.band,
            "cases": self.cases,
            "classification_correct": self.classification_correct,
            "severity_correct": self.severity_correct,
            "classification_lb": round(self.classification_lb, 6),
            "severity_lb": round(self.severity_lb, 6),
        }


def band_scores(outcomes: Sequence[CaseOutcome]) -> dict[str, BandScore]:
    """One score per band, counted over **cases** (never samples)."""
    out: dict[str, BandScore] = {}
    for band in BANDS:
        members = [o for o in outcomes if o.band == band]
        out[band] = BandScore(
            band=band,
            cases=len(members),
            classification_correct=sum(o.classification_correct for o in members),
            severity_correct=sum(o.severity_correct for o in members),
        )
    return out


def cost_total(outcomes: Sequence[CaseOutcome]) -> int:
    return sum(o.cost for o in outcomes)


@dataclass(frozen=True, slots=True)
class QualityGate:
    """One gate: ``metric`` (classification | severity) lower bound ≥ ``min_lb`` in ``band``."""

    name: str
    band: str
    metric: str
    min_lb: float
    min_cases: int


#: ARCH §11 (prov.): P1/P2 classification LB ≥ 0.90, P1/P2 severity LB ≥ 0.85.
DEFAULT_GATES: tuple[QualityGate, ...] = (
    QualityGate("p1p2_classification", "P1-P2", "classification", 0.90, 35),
    QualityGate("p1p2_severity", "P1-P2", "severity", 0.85, 22),
)


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    value: float | None
    reason: str


def evaluate_gates(
    scores: Mapping[str, BandScore], gates: Sequence[QualityGate] = DEFAULT_GATES
) -> list[GateResult]:
    results: list[GateResult] = []
    for gate in gates:
        score = scores.get(gate.band)
        if score is None or score.cases < gate.min_cases:
            n = score.cases if score else 0
            results.append(
                GateResult(
                    gate.name, False, None, f"{n} {gate.band} cases; needs >= {gate.min_cases}"
                )
            )
            continue
        lb = score.classification_lb if gate.metric == "classification" else score.severity_lb
        ok = lb >= gate.min_lb
        results.append(
            GateResult(
                gate.name,
                ok,
                round(lb, 6),
                f"lower bound {lb:.3f} {'>=' if ok else '<'} {gate.min_lb:.2f}",
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class CaseDiff:
    newly_failing: list[str] = field(default_factory=list)
    newly_passing: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def diff_against_baseline(current: Mapping[str, bool], baseline: Mapping[str, bool]) -> CaseDiff:
    """Per case (``case_id → passed``): what changed since the baseline report."""
    shared = set(current) & set(baseline)
    return CaseDiff(
        newly_failing=sorted(c for c in shared if baseline[c] and not current[c]),
        newly_passing=sorted(c for c in shared if not baseline[c] and current[c]),
        added=sorted(set(current) - set(baseline)),
        removed=sorted(set(baseline) - set(current)),
    )


@dataclass(frozen=True, slots=True)
class SplitCase:
    case_id: str
    opened_at: datetime
    signature: str | None


def split_violations(train: Sequence[SplitCase], holdout: Sequence[SplitCase]) -> list[str]:
    """ARCH §10.1: train < T < holdout in time, and no shared signature."""
    problems: list[str] = []
    if train and holdout:
        last_train = max(c.opened_at for c in train)
        early = sorted(c.case_id for c in holdout if c.opened_at <= last_train)
        if early:
            problems.append(f"holdout cases not after the last training case: {', '.join(early)}")
    train_sigs = {c.signature for c in train if c.signature}
    shared = sorted(c.case_id for c in holdout if c.signature and c.signature in train_sigs)
    if shared:
        problems.append(f"holdout cases sharing a training signature: {', '.join(shared)}")
    return problems


__all__ = [
    "BANDS",
    "COST",
    "DEFAULT_GATES",
    "BandScore",
    "CaseDiff",
    "CaseOutcome",
    "GateResult",
    "QualityGate",
    "SplitCase",
    "band_scores",
    "cost_total",
    "diff_against_baseline",
    "evaluate_gates",
    "majority",
    "split_violations",
]
