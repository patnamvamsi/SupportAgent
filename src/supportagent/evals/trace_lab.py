"""The trace lab (ADR-0021 §6): labelled questions per flow, with recorded logs.

Each scenario is one object's logs across a flow's components, the moment it
is asked about, the log stores that are down, the personal data seeded into
its lines, and the truth (the verdict, the stage, the late stages). The lab
traces it, explains it, and scores:

- **wrong stage**: the trace says the object stopped (stuck, rejected, not
  found) somewhere it didn't, or calls it stopped when it isn't. The gate
  allows none: it is the trace's false cause;
- **deviation found**: the share of stuck objects found stuck at the right stage;
- **can't tell names the stage**: every ``cannot_tell`` names its stage and why;
- **PII leaks**: a seeded value in any model request. The gate allows none;
- **answers kept**: the share of scenarios where at least one business sentence
  survived its check, gated at a floor set from live recordings.

Like the other labs, CI replays recorded model answers; ``--live`` records.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal["complete", "rejected", "in_progress", "stuck", "not_found", "cannot_tell"]
#: Verdicts that say where an object stopped: the claims the wrong-stage gate scores.
STOPPED: frozenset[str] = frozenset({"stuck", "rejected", "not_found"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeededPii(_Strict):
    value: str = Field(min_length=1)
    entity: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class TraceTruth(_Strict):
    kind: Kind
    stage: str | None = None
    late: list[str] = Field(default_factory=list)


class TraceScenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    object_id: str
    now: str
    #: Per service, its log lines for this object: ``[timestamp, line]``.
    logs: dict[str, list[list[str]]] = Field(default_factory=dict)
    #: Services whose log store fails to answer.
    down: list[str] = Field(default_factory=list)
    pii: list[SeededPii] = Field(default_factory=list)
    truth: TraceTruth


class TraceSuite(_Strict):
    flow: str
    scenarios: list[TraceScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> TraceSuite:
        ids = [s.id for s in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate trace scenario ids")  # noqa: TRY003
        return self


class TraceGates(_Strict):
    min_cases: int = 20
    max_wrong_stage: int = Field(default=0, ge=0)
    min_deviation_found: float = Field(default=0.90, ge=0.0, le=1.0)
    max_pii_leaks: int = Field(default=0, ge=0)
    #: The floor is set in ``evals/gates.yaml`` from live recordings.
    min_answers_kept: float = Field(default=0.0, ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class TraceOutcome:
    """What one scenario's trace and answer produced, as the lab scores it."""

    kind: str
    stage: str | None
    late: tuple[str, ...]
    reason: str
    #: Every text sent to the model while answering it.
    requests: tuple[str, ...]
    business_kept: int
    dropped: int


@dataclass(frozen=True, slots=True)
class TraceScore:
    scenario_id: str
    expected: TraceTruth
    got: TraceOutcome
    leaks: tuple[str, ...]

    @property
    def verdict_ok(self) -> bool:
        t = self.expected
        return (
            self.got.kind == t.kind and self.got.stage == t.stage and list(self.got.late) == t.late
        )

    @property
    def wrong_stage(self) -> bool:
        claims = self.got.kind in STOPPED
        truly = self.expected.kind in STOPPED
        if claims:
            return (
                not truly
                or self.got.stage != self.expected.stage
                or self.got.kind != self.expected.kind
            )
        return truly and self.got.kind not in ("cannot_tell",)

    @property
    def names_stage(self) -> bool:
        return self.got.kind != "cannot_tell" or bool(self.got.stage and self.got.reason)

    @property
    def ok(self) -> bool:
        return self.verdict_ok and not self.leaks and self.names_stage

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.scenario_id,
            "ok": self.ok,
            "expected": self.expected.model_dump(),
            "got": {
                "kind": self.got.kind,
                "stage": self.got.stage,
                "late": list(self.got.late),
                "business_kept": self.got.business_kept,
                "dropped": self.got.dropped,
            },
            "wrong_stage": self.wrong_stage,
            "leaks": len(self.leaks),
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class TraceReport:
    flow: str
    mode: str
    model_id: str
    scores: list[TraceScore]
    gates: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.ok for g in self.gates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow,
            "mode": self.mode,
            "model_id": self.model_id,
            "passed": self.passed,
            "scores": [s.as_dict() for s in self.scores],
            "gates": [{"name": g.name, "ok": g.ok, "detail": g.detail} for g in self.gates],
        }


Runner = Callable[[TraceScenario], TraceOutcome]


def run_trace_lab(
    suite: TraceSuite, runner: Runner, *, gates: TraceGates, mode: str, model_id: str
) -> TraceReport:
    scores = [_score(s, runner(s)) for s in suite.scenarios]
    return TraceReport(suite.flow, mode, model_id, scores, _gates(scores, gates))


def _score(scenario: TraceScenario, got: TraceOutcome) -> TraceScore:
    sent = "\n".join(got.requests)
    leaks = tuple(p.value for p in scenario.pii if p.value in sent)
    return TraceScore(scenario.id, scenario.truth, got, leaks)


def _gates(scores: Sequence[TraceScore], gates: TraceGates) -> list[GateResult]:
    n = len(scores)
    wrong = [s.scenario_id for s in scores if s.wrong_stage]
    stuck = [s for s in scores if s.expected.kind == "stuck"]
    found = sum(1 for s in stuck if s.got.kind == "stuck" and s.got.stage == s.expected.stage)
    rate = found / len(stuck) if stuck else 1.0
    unnamed = [s.scenario_id for s in scores if not s.names_stage]
    leaks = [s.scenario_id for s in scores if s.leaks]
    kept = sum(1 for s in scores if s.got.business_kept > 0) / n if n else 0.0
    return [
        GateResult(
            "min_cases", n >= gates.min_cases, f"{n} scenario(s), at least {gates.min_cases}"
        ),
        GateResult(
            "wrong_stage",
            len(wrong) <= gates.max_wrong_stage,
            f"{len(wrong)} wrong stopping stage(s)" + (f": {', '.join(wrong)}" if wrong else ""),
        ),
        GateResult(
            "deviation_found",
            rate >= gates.min_deviation_found,
            f"{found}/{len(stuck)} stuck object(s) found at the right stage "
            f"({rate:.2f}, at least {gates.min_deviation_found:.2f})",
        ),
        GateResult(
            "cannot_tell_names_stage",
            not unnamed,
            "every can't-tell names its stage" if not unnamed else f"unnamed: {', '.join(unnamed)}",
        ),
        GateResult(
            "pii_leaks",
            len(leaks) <= gates.max_pii_leaks,
            f"{len(leaks)} scenario(s) sent seeded PII to the model"
            + (f": {', '.join(leaks)}" if leaks else ""),
        ),
        GateResult(
            "answers_kept",
            kept >= gates.min_answers_kept,
            f"{kept:.2f} of scenarios kept a business sentence "
            f"(at least {gates.min_answers_kept:.2f})",
        ),
    ]


__all__ = [
    "GateResult",
    "SeededPii",
    "TraceGates",
    "TraceOutcome",
    "TraceReport",
    "TraceScenario",
    "TraceScore",
    "TraceSuite",
    "TraceTruth",
    "run_trace_lab",
]
