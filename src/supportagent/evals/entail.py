"""Citation faithfulness (BL-039, ARCH §5.4/§11): the ``entail`` checker, regression-tested.

RCA-lite sends every claim, with the tool results it cites, through the
``entail`` slot; anything but ``supports`` makes the claim speculation. This
suite pins that checker on curated claim/evidence pairs, in the exact shape
RCA sends (``<hypothesis>: <check> -> true`` and the cited results as JSON):

- **faithfulness** is the share of pairs judged as the curator expects
  (release gate >= 0.95, provisional);
- a **false support** (``supports`` where the evidence doesn't show the claim)
  is the dangerous error: it would make a claim sendable. The gate allows none.

No answer (a provider failure, an unparseable reply) counts as ``unknown``,
as it does in RCA. Pairs are curated from the lab's tool worlds now, and from
the partner's incidents when they arrive (BL-037).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Verdict = Literal["supports", "contradicts", "unknown"]
VERDICTS: tuple[Verdict, ...] = ("supports", "contradicts", "unknown")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntailCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    #: As RCA writes it: ``<hypothesis>: <check expression> -> true``.
    claim: str = Field(min_length=1)
    #: The cited tool results, as RCA passes them.
    evidence: list[Any] = Field(min_length=1)
    expected: Verdict
    why: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class EntailSuite(_Strict):
    cases: list[EntailCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> EntailSuite:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate entail case ids")  # noqa: TRY003
        return self


class EntailGates(_Strict):
    min_cases: int = 20
    min_faithfulness: float = Field(default=0.95, ge=0.0, le=1.0)
    max_false_support: int = Field(default=0, ge=0)


@dataclass(frozen=True, slots=True)
class EntailScore:
    case_id: str
    expected: Verdict
    got: Verdict
    #: Every sample's verdict (``got`` is the majority, ties to ``unknown``).
    samples: tuple[Verdict, ...]

    @property
    def ok(self) -> bool:
        return self.got == self.expected

    @property
    def false_support(self) -> bool:
        """``supports`` in **any** sample where the evidence doesn't show the claim."""
        return self.expected != "supports" and "supports" in self.samples


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    ok: bool
    detail: str


@dataclass
class EntailReport:
    mode: str
    model_id: str
    samples: int
    scores: list[EntailScore]
    gates: list[GateResult] = field(default_factory=list)

    @property
    def faithfulness(self) -> float:
        return sum(s.ok for s in self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def false_supports(self) -> list[str]:
        return [s.case_id for s in self.scores if s.false_support]

    @property
    def passed(self) -> bool:
        return all(g.ok for g in self.gates)

    def by_expected(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for verdict in VERDICTS:
            mine = [s for s in self.scores if s.expected == verdict]
            out[verdict] = {"cases": len(mine), "ok": sum(s.ok for s in mine)}
        return out

    def as_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "mode": self.mode,
            "model_id": self.model_id,
            "samples": self.samples,
            "faithfulness": round(self.faithfulness, 3),
            "false_supports": self.false_supports,
            "by_expected": self.by_expected(),
            "cases": [
                {"id": s.case_id, "expected": s.expected, "got": s.got, "samples": list(s.samples)}
                for s in self.scores
            ],
            "gates": [{"name": g.name, "ok": g.ok, "detail": g.detail} for g in self.gates],
        }


def majority(samples: Sequence[Verdict]) -> Verdict:
    """The most common verdict; a tie is ``unknown`` (fail closed)."""
    counts = Counter(samples).most_common()
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return "unknown"
    return counts[0][0]


def run_entail(  # noqa: PLR0913 — the cases, the judge and the run's labels
    cases: Sequence[EntailCase],
    judge: Callable[[EntailCase, int], Verdict | None],
    *,
    mode: str,
    model_id: str,
    samples: int = 1,
    gates: EntailGates | None = None,
) -> EntailReport:
    scores: list[EntailScore] = []
    for case in cases:
        got = tuple(judge(case, n) or "unknown" for n in range(samples))
        scores.append(EntailScore(case.id, case.expected, majority(got), got))
    report = EntailReport(mode=mode, model_id=model_id, samples=samples, scores=scores)
    evaluate(report, gates or EntailGates())
    return report


def evaluate(report: EntailReport, gates: EntailGates) -> None:
    n = len(report.scores)
    report.gates = [
        GateResult("min_cases", n >= gates.min_cases, f"{n} case(s) (min {gates.min_cases})"),
        GateResult(
            "faithfulness",
            report.faithfulness >= gates.min_faithfulness,
            f"{report.faithfulness:.3f} (min {gates.min_faithfulness})",
        ),
        GateResult(
            "false_support",
            len(report.false_supports) <= gates.max_false_support,
            f"{len(report.false_supports)} {report.false_supports} (max {gates.max_false_support})",
        ),
    ]


__all__ = [
    "EntailCase",
    "EntailGates",
    "EntailReport",
    "EntailScore",
    "EntailSuite",
    "Verdict",
    "evaluate",
    "majority",
    "run_entail",
]
