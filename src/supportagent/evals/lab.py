"""The incident lab (ADR-0011 §5, WP-M02): scenarios with known root causes.

A **scenario** is an incident, the service it hit (with its catalog ``stack``),
the **world** each tool capability returns, the team **docs** RCA may read
(WP-M08: helpful, wrong, stale or injected), and the **truth**: the label, the
severity, the root-cause hypothesis, the verdict RCA should reach, the tools
the conclusion must cite, and traps it must not claim.

The lab runs the real classifier and the real RCA service; only the tools
are world-backed (:class:`WorldTools`). Scoring is structural: typed outputs
compared with the truth, no model grading a model. The strictest metric is
the **false-cause rate**: SUPPORTED on the wrong hypothesis.

Pure: scenario loading reads YAML through the caller; the wiring (the LLM and
the knowledge) is ``bootstrap.LabPipeline``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from supportagent.domain.classification import prefix_match
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.knowledge import DocMeta
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.model.knowledge import SopMatter
from supportagent.domain.trace import TraceContext
from supportagent.ports.tools import ToolResult

Verdict = Literal["SUPPORTED", "CONSISTENT_UNCONFIRMED", "INCONCLUSIVE"]
Split = Literal["tune", "holdout"]
_Z = 1.959963984540054


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabIncident(_Strict):
    subject: str = Field(min_length=1)
    body: str = ""
    #: The source priority, 1-5 (ADR-0009: severity comes from it).
    priority: str = Field(default="3", pattern=r"^[1-5]$")
    origin: Literal["alert", "ticket"] = "alert"
    alert_type: str | None = None
    error_signature: str | None = None


class WorldEntry(_Strict):
    """What one tool capability returns in this scenario: a result or an error."""

    result: dict[str, Any] | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _one(self) -> WorldEntry:
        if (self.result is None) == (self.error is None):
            raise ValueError("a world entry has exactly one of result or error")  # noqa: TRY003
        return self


class LabDoc(_Strict):
    """A team doc in the scenario's knowledge (WP-M08), as the markdown source would load it."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    space: str = "wiki"
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    visibility: Literal["internal", "customer"] = "internal"
    last_updated: date | None = None
    services: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list, max_length=5)
    #: ADR-0014: the doc's procedure, as the markdown source would load it.
    sop: SopMatter | None = None

    def meta(self, redact: Callable[[str], str]) -> DocMeta:
        return DocMeta(
            doc_id=self.id,
            space=self.space,
            title=self.title,
            version="lab",
            last_updated=self.last_updated,
            visibility=self.visibility,
            services=tuple(self.services),
            hypotheses=tuple(self.hypotheses),
            sop=self.sop.sop(redact) if self.sop is not None else None,
        )


class LabTruth(_Strict):
    classification: str = Field(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    #: Other labels a careful analyst could defensibly pick for this ticket text.
    also_acceptable: list[str] = Field(default_factory=list)
    severity: Literal["P1", "P2", "P3", "P4"]
    #: The hypothesis that explains the incident; ``None``: none is findable.
    root_cause: str | None
    verdict: Verdict
    must_cite: list[str] = Field(default_factory=list)
    must_not_claim: list[str] = Field(default_factory=list)
    #: WP-M08: the doc ids a good search returns for this incident (recall@k).
    guidance: list[str] = Field(default_factory=list)
    #: ADR-0014: exactly the ``<doc>/<step>`` SOP steps RCA should propose;
    #: ``None``: not checked.
    sop_steps: list[str] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> LabTruth:
        if self.verdict == "SUPPORTED" and self.root_cause is None:
            raise ValueError("verdict SUPPORTED needs a root_cause")  # noqa: TRY003
        if self.root_cause is not None and self.root_cause in self.must_not_claim:
            raise ValueError("the root cause can't also be a trap")  # noqa: TRY003
        return self


class LabScenario(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    pack: str
    split: Split = "tune"
    description: str = Field(min_length=1)
    incident: LabIncident
    service: dict[str, Any]
    world: dict[str, WorldEntry] = Field(default_factory=dict)
    docs: list[LabDoc] = Field(default_factory=list)
    truth: LabTruth
    why: str = Field(min_length=1)

    @model_validator(mode="after")
    def _guidance_known(self) -> LabScenario:
        ids = [d.id for d in self.docs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate doc ids")  # noqa: TRY003
        unknown = sorted(set(self.truth.guidance) - set(ids))
        if unknown:
            raise ValueError(f"truth.guidance names unknown docs {unknown}")  # noqa: TRY003
        steps = {f"{d.id}/{s.id}" for d in self.docs if d.sop is not None for s in d.sop.steps}
        unknown = sorted(set(self.truth.sop_steps or []) - steps)
        if unknown:
            raise ValueError(f"truth.sop_steps names unknown SOP steps {unknown}")  # noqa: TRY003
        return self

    @property
    def service_entry(self) -> ServiceEntry:
        return ServiceEntry.model_validate(self.service)


def load_scenarios(
    directory: Path,
    *,
    pack: str,
    tool_schemas: Mapping[str, Mapping[str, object]],
    read_yaml: Any,  # noqa: ANN401 — the adapters' YAML loader, injected
) -> list[LabScenario]:
    """Every ``*.yaml`` scenario under ``directory``, validated (world vs tool schemas)."""
    out: list[LabScenario] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "baseline.yaml":
            continue
        try:
            scenario = LabScenario.model_validate(read_yaml(path))
        except ValidationError as exc:
            raise ConfigError(f"{path}: {exc}") from exc  # noqa: TRY003
        if scenario.pack != pack:
            raise ConfigError(f"{path}: pack {scenario.pack!r}, expected {pack!r}")  # noqa: TRY003
        for name, entry in scenario.world.items():
            if name not in tool_schemas:
                raise ConfigError(f"{path}: world names unknown tool {name!r}")  # noqa: TRY003
            if entry.result is not None:
                check_shape(path, name, entry.result, tool_schemas[name])
        out.append(scenario)
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{directory}: duplicate scenario ids")  # noqa: TRY003
    return out


def check_shape(
    path: Path, tool: str, result: Mapping[str, Any], schema: Mapping[str, object]
) -> None:
    """The world result carries every property the tool's schema requires."""
    required = schema.get("required")
    missing = [k for k in required if k not in result] if isinstance(required, list) else []
    if missing:
        raise ConfigError(  # noqa: TRY003
            f"{path}: world result for {tool!r} misses {missing} (its result schema)"
        )


class WorldTools:
    """The RCA ``_Tools`` port, answering from a scenario's world (args ignored)."""

    def __init__(self, world: Mapping[str, WorldEntry]) -> None:
        self._world = dict(world)
        self.calls: list[str] = []

    @classmethod
    def of(cls, scenario: LabScenario) -> WorldTools:
        return cls(scenario.world)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._world)

    def call(
        self,
        name: str,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
    ) -> ToolResult:
        self.calls.append(name)
        entry = self._world.get(name)
        if entry is None:
            raise ToolError(f"{name}: not available in this scenario")  # noqa: TRY003
        if entry.error is not None:
            raise ToolError(entry.error)
        return ToolResult(data=dict(entry.result or {}))


# --- one run -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabOutcome:
    """What the system concluded for one scenario (one sample)."""

    scenario_id: str
    classification: str | None
    severity: str | None
    preclass_label: str | None
    verdict: str
    supported: str | None
    tested: tuple[str, ...]
    cited_tools: frozenset[str]
    tool_calls: int
    tokens: int
    reason: str = ""
    #: WP-M08: the doc ids the knowledge search returned.
    retrieved: tuple[str, ...] = ()
    #: ADR-0014: the ``<doc>/<step>`` SOP steps RCA would propose.
    sop_steps: tuple[str, ...] = ()


class LabPipeline(Protocol):
    def run(self, scenario: LabScenario, sample: int) -> LabOutcome: ...


@dataclass(frozen=True, slots=True)
class LabScore:
    scenario_id: str
    split: str
    classification_ok: bool
    severity_ok: bool
    verdict_ok: bool
    root_cause_ok: bool
    false_cause: bool
    trap_hit: bool
    citations_ok: bool
    abstained_correctly: bool | None
    tool_calls: int
    tokens: int
    failures: tuple[str, ...]
    #: WP-M08: share of the truth's guidance docs retrieved; ``None``: none expected.
    retrieval_recall: float | None = None
    #: ADR-0014: the proposed SOP steps equal the truth's; ``None``: not checked.
    sop_ok: bool | None = None

    @property
    def passed(self) -> bool:
        return (
            self.classification_ok
            and self.verdict_ok
            and self.root_cause_ok
            and self.citations_ok
            and not self.false_cause
            and self.sop_ok is not False
        )


def score_outcome(scenario: LabScenario, out: LabOutcome) -> LabScore:
    truth = scenario.truth
    failures: list[str] = []
    classification_ok = out.classification is not None and any(
        prefix_match(label, out.classification)
        for label in (truth.classification, *truth.also_acceptable)
    )
    if not classification_ok:
        failures.append(f"classification {out.classification} != {truth.classification}")
    severity_ok = out.severity == truth.severity
    if not severity_ok:
        failures.append(f"severity {out.severity} != {truth.severity}")
    verdict_ok = out.verdict == truth.verdict
    if not verdict_ok:
        failures.append(f"verdict {out.verdict} != {truth.verdict}")
    supported = out.supported if out.verdict == "SUPPORTED" else None
    false_cause = supported is not None and supported != truth.root_cause
    if false_cause:
        failures.append(f"FALSE CAUSE: SUPPORTED {supported}, truth {truth.root_cause}")
    trap_hit = supported is not None and supported in truth.must_not_claim
    if trap_hit:
        failures.append(f"claimed the trap {supported}")
    if truth.verdict == "SUPPORTED":
        root_cause_ok = supported == truth.root_cause
    else:
        root_cause_ok = supported is None
    if not root_cause_ok and not false_cause:
        failures.append(f"root cause {supported} != {truth.root_cause}")
    missing = sorted(set(truth.must_cite) - out.cited_tools)
    citations_ok = not missing or out.verdict != "SUPPORTED"
    if not citations_ok:
        failures.append(f"missing citations {missing}")
    # Abstention: no cause is findable, and the system claimed none (whichever
    # non-SUPPORTED verdict the truth expects).
    abstained = (out.verdict == truth.verdict) if truth.root_cause is None else None
    recall = None
    if truth.guidance:
        found = set(truth.guidance) & set(out.retrieved)
        recall = len(found) / len(truth.guidance)
        if recall < 1.0:
            failures.append(f"docs not retrieved: {sorted(set(truth.guidance) - found)}")
    sop_ok = _sop_check(truth, out, failures)
    return LabScore(
        scenario_id=scenario.id,
        split=scenario.split,
        classification_ok=classification_ok,
        severity_ok=severity_ok,
        verdict_ok=verdict_ok,
        root_cause_ok=root_cause_ok,
        false_cause=false_cause,
        trap_hit=trap_hit,
        citations_ok=citations_ok,
        abstained_correctly=abstained,
        tool_calls=out.tool_calls,
        tokens=out.tokens,
        failures=tuple(failures),
        retrieval_recall=recall,
        sop_ok=sop_ok,
    )


def _sop_check(truth: LabTruth, out: LabOutcome, failures: list[str]) -> bool | None:
    if truth.sop_steps is None:
        return None
    ok = sorted(out.sop_steps) == sorted(truth.sop_steps)
    if not ok:
        failures.append(f"SOP steps {sorted(out.sop_steps)} != {sorted(truth.sop_steps)}")
    return ok


def majority(scores: Sequence[LabScore]) -> LabScore:
    """N samples of one scenario → one score: each flag by majority (ARCH §11).

    A false cause counts if it happened in **any** sample: it's the error the
    gate exists to catch, and one confident wrong answer in three is not safe.
    """
    if len(scores) == 1:
        return scores[0]
    half = len(scores) / 2

    def most(flag: str) -> bool:
        return sum(bool(getattr(s, flag)) for s in scores) > half

    first = scores[0]
    abstain = [s.abstained_correctly for s in scores if s.abstained_correctly is not None]
    return LabScore(
        scenario_id=first.scenario_id,
        split=first.split,
        classification_ok=most("classification_ok"),
        severity_ok=most("severity_ok"),
        verdict_ok=most("verdict_ok"),
        root_cause_ok=most("root_cause_ok"),
        false_cause=any(s.false_cause for s in scores),
        trap_hit=any(s.trap_hit for s in scores),
        citations_ok=most("citations_ok"),
        abstained_correctly=(sum(abstain) > len(abstain) / 2) if abstain else None,
        tool_calls=round(sum(s.tool_calls for s in scores) / len(scores)),
        tokens=round(sum(s.tokens for s in scores) / len(scores)),
        failures=tuple(dict.fromkeys(f for s in scores for f in s.failures)),
        retrieval_recall=(
            sum(r for r in recalls) / len(recalls)
            if (recalls := [s.retrieval_recall for s in scores if s.retrieval_recall is not None])
            else None
        ),
        sop_ok=(
            sum(checked) > len(checked) / 2
            if (checked := [s.sop_ok for s in scores if s.sop_ok is not None])
            else None
        ),
    )


# --- the report and its gates ---------------------------------------------------------


def wilson_lb(successes: int, n: int) -> float:
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + _Z * _Z / n
    centre = p + _Z * _Z / (2 * n)
    margin = _Z * math.sqrt(p * (1 - p) / n + _Z * _Z / (4 * n * n))
    return (centre - margin) / denom


class LabGates(_Strict):
    """Per-pack gates (``evals/gates.yaml`` ``lab:``); every number provisional."""

    min_cases: int = Field(default=10, ge=1)
    min_classification_lb: float = 0.70
    min_root_cause_lb: float = 0.60
    max_false_cause_rate: float = 0.0
    #: A scenario that passed in the baseline must still pass.
    no_regressions: bool = True
    #: WP-M08: mean recall@k of the guidance docs, over scenarios that expect any.
    min_retrieval_recall: float = Field(default=0.0, ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    ok: bool
    detail: str


@dataclass
class LabReport:
    pack: str
    mode: str
    samples: int
    model_id: str
    scores: list[LabScore]
    gates: list[GateCheck] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.ok for g in self.gates)

    def retrieval_recall(self) -> tuple[float | None, int]:
        recalls = [s.retrieval_recall for s in self.scores if s.retrieval_recall is not None]
        return (sum(recalls) / len(recalls) if recalls else None), len(recalls)

    def rate(self, flag: str) -> tuple[int, int]:
        return sum(bool(getattr(s, flag)) for s in self.scores), len(self.scores)

    def metrics(self) -> dict[str, object]:
        n = len(self.scores)
        cls_ok, _ = self.rate("classification_ok")
        rc_ok, _ = self.rate("root_cause_ok")
        false_causes, _ = self.rate("false_cause")
        cite_ok, _ = self.rate("citations_ok")
        abstain = [s.abstained_correctly for s in self.scores if s.abstained_correctly is not None]
        return {
            "cases": n,
            "passed": sum(s.passed for s in self.scores),
            "classification": {"rate": _r(cls_ok, n), "wilson_lb": round(wilson_lb(cls_ok, n), 4)},
            "severity": {"rate": _r(self.rate("severity_ok")[0], n)},
            "root_cause": {"rate": _r(rc_ok, n), "wilson_lb": round(wilson_lb(rc_ok, n), 4)},
            "false_cause": {"count": false_causes, "rate": _r(false_causes, n)},
            "traps_hit": self.rate("trap_hit")[0],
            "citations": {"rate": _r(cite_ok, n)},
            "abstention": {"rate": _r(sum(abstain), len(abstain)), "cases": len(abstain)},
            "retrieval_recall": {
                "mean": round(r, 4) if (r := self.retrieval_recall()[0]) is not None else None,
                "cases": self.retrieval_recall()[1],
            },
            "mean_tool_calls": _mean([s.tool_calls for s in self.scores]),
            "mean_tokens": _mean([s.tokens for s in self.scores]),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "pack": self.pack,
            "mode": self.mode,
            "samples": self.samples,
            "model_id": self.model_id,
            "passed": self.passed,
            "metrics": self.metrics(),
            "gates": [{"name": g.name, "ok": g.ok, "detail": g.detail} for g in self.gates],
            "regressions": self.regressions,
            "improvements": self.improvements,
            "cases": [
                {"id": s.scenario_id, "split": s.split, "passed": s.passed, "failures": s.failures}
                for s in self.scores
            ],
        }


def _r(k: int, n: int) -> float | None:
    return round(k / n, 4) if n else None


def _mean(values: Sequence[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def evaluate(report: LabReport, gates: LabGates, baseline: Mapping[str, bool] | None) -> None:
    """Apply the gates and the per-case diff to ``report`` in place."""
    n = len(report.scores)
    cls_ok, _ = report.rate("classification_ok")
    rc_ok, _ = report.rate("root_cause_ok")
    false_causes, _ = report.rate("false_cause")
    checks = [
        GateCheck(
            "false_cause_rate",
            (false_causes / n if n else 0.0) <= gates.max_false_cause_rate,
            f"{false_causes}/{n} false causes (max rate {gates.max_false_cause_rate})",
        )
    ]
    if n >= gates.min_cases:
        cls_lb, rc_lb = wilson_lb(cls_ok, n), wilson_lb(rc_ok, n)
        checks.append(
            GateCheck(
                "classification_lb",
                cls_lb >= gates.min_classification_lb,
                f"{cls_lb:.3f} (min {gates.min_classification_lb})",
            )
        )
        checks.append(
            GateCheck(
                "root_cause_lb",
                rc_lb >= gates.min_root_cause_lb,
                f"{rc_lb:.3f} (min {gates.min_root_cause_lb})",
            )
        )
    else:
        checks.append(
            GateCheck("min_cases", True, f"{n} < {gates.min_cases}: lower-bound gates not applied")
        )
    recall, recall_cases = report.retrieval_recall()
    if recall is not None:
        checks.append(
            GateCheck(
                "retrieval_recall",
                recall >= gates.min_retrieval_recall,
                f"{recall:.3f} over {recall_cases} case(s) (min {gates.min_retrieval_recall})",
            )
        )
    if baseline is not None:
        now = {s.scenario_id: s.passed for s in report.scores}
        report.regressions = sorted(k for k, ok in baseline.items() if ok and now.get(k) is False)
        report.improvements = sorted(k for k, ok in now.items() if ok and baseline.get(k) is False)
        if gates.no_regressions:
            checks.append(
                GateCheck(
                    "no_regressions",
                    not report.regressions,
                    f"{len(report.regressions)} regression(s) vs baseline",
                )
            )
    report.gates = checks


def select_split(
    scenarios: Sequence[LabScenario], split: Literal["tune", "holdout", "all"]
) -> list[LabScenario]:
    """``tune`` runs never see the held-out scenarios (ARCH §11)."""
    if split == "all":
        return list(scenarios)
    return [s for s in scenarios if s.split == split]


def run_lab(  # noqa: PLR0913 — the run's inputs
    scenarios: Sequence[LabScenario],
    pipeline: LabPipeline,
    *,
    pack: str,
    mode: str,
    model_id: str,
    samples: int = 1,
    gates: LabGates | None = None,
    baseline: Mapping[str, bool] | None = None,
) -> LabReport:
    scores = [
        majority([score_outcome(s, pipeline.run(s, i)) for i in range(samples)]) for s in scenarios
    ]
    report = LabReport(pack=pack, mode=mode, samples=samples, model_id=model_id, scores=scores)
    evaluate(report, gates or LabGates(), baseline)
    return report


def baseline_of(report: LabReport) -> dict[str, bool]:
    return {s.scenario_id: s.passed for s in report.scores}


def write_lab_report(directory: Path, report: LabReport, *, now: datetime) -> Path:
    run = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}-lab-{report.pack}-{report.mode}"
    run.mkdir(parents=True, exist_ok=True)
    path = run / "report.json"
    path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def read_baseline(path: Path) -> dict[str, bool] | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = raw.get("cases", {}) if isinstance(raw, dict) else {}
    return {str(k): bool(v) for k, v in cases.items()}


def write_baseline(path: Path, report: LabReport) -> None:
    body = {
        "v": 1,
        "pack": report.pack,
        "model_id": report.model_id,
        "samples": report.samples,
        "cases": baseline_of(report),
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cited_tools(evidence: Sequence[Any], cites: Collection[str]) -> frozenset[str]:
    """The tools behind the cited steps (``Claim.cites`` holds step ids)."""
    by_step = {e.step.step_id: e.tool for e in evidence}
    return frozenset(by_step[c] for c in cites if c in by_step)


__all__ = [
    "GateCheck",
    "LabDoc",
    "LabGates",
    "LabIncident",
    "LabOutcome",
    "LabPipeline",
    "LabReport",
    "LabScenario",
    "LabScore",
    "LabTruth",
    "WorldEntry",
    "WorldTools",
    "baseline_of",
    "check_shape",
    "cited_tools",
    "evaluate",
    "load_scenarios",
    "majority",
    "read_baseline",
    "run_lab",
    "score_outcome",
    "select_split",
    "wilson_lb",
    "write_baseline",
    "write_lab_report",
]
