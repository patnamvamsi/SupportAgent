"""The smoke harness (10-wp WP-016, 04 §8): run cases, score them, write the report.

Each case goes through the shadow pipeline that ``bootstrap`` builds
(``EvalPipeline``): the same pre-classifier, classifier, routing and
proposal builder as production, on a throwaway database. ``fixture`` mode
scripts the LLM per case and tests **wiring**. ``live`` mode calls the
configured model, and is what ``--write-calibration`` measures.

The gate: in fixture mode every case must pass, since the run is
deterministic. In live mode only ``invariant``-tagged cases gate; the rest
are reported, because the smoke suite is not a quality gate (ROADMAP §2.1).
A case whose first delivery links to an earlier case is a fingerprint
collision in the suite itself, and fails.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from supportagent.domain.model.alert import AlertEvent
from supportagent.evals.cases import EvalCase
from supportagent.evals.scoring import CaseRun, CaseScore, SuiteScore, score, summarize

INVARIANT_TAG = "invariant"


class SuiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: str | None = None
    description: str = ""


class Manifest(BaseModel):
    """``evals/manifest.yaml``: what a run pins (ARCH §11 snapshot) and the suites."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1)
    snapshot_id: str = Field(min_length=1)
    config: str = Field(min_length=1)
    catalog: str = Field(min_length=1)
    cases: str = Field(min_length=1)
    suites: dict[str, SuiteSpec]


class Pipeline(Protocol):
    mode: Literal["fixture", "live"]
    model_id: str

    def run(self, case: EvalCase, evt: AlertEvent) -> CaseRun: ...


@dataclass(frozen=True, slots=True)
class SuiteRun:
    score: SuiteScore
    pairs: list[tuple[EvalCase, CaseRun]]


def select(cases: Sequence[EvalCase], spec: SuiteSpec) -> list[EvalCase]:
    return [c for c in cases if spec.tag is None or spec.tag in c.tags]


def run_suite(suite: str, cases: Sequence[EvalCase], pipeline: Pipeline) -> SuiteRun:
    scores: list[CaseScore] = []
    pairs: list[tuple[EvalCase, CaseRun]] = []
    skipped: list[str] = []
    for index, case in enumerate(cases):
        if pipeline.mode not in case.modes:
            skipped.append(case.id)
            continue
        run = pipeline.run(case, case.alert(index))
        gate = pipeline.mode == "fixture" or INVARIANT_TAG in case.tags
        result = score(case, run, gate=gate)
        if case.repeat == 1 and run.linked:
            result = CaseScore(
                case.id,
                False,
                [*result.failures, "fingerprint collides with an earlier case in the suite"],
                gate,
            )
        scores.append(result)
        pairs.append((case, run))
    return SuiteRun(score=summarize(suite, pipeline.mode, scores, skipped), pairs=pairs)


def write_report(
    artifacts: Path, suite_run: SuiteRun, *, manifest: Manifest, model_id: str, now: datetime
) -> Path:
    """``evals/artifacts/<run>/report.json`` (02 §4)."""
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{suite_run.score.suite}-{suite_run.score.mode}"
    out = artifacts / run_id
    out.mkdir(parents=True, exist_ok=True)
    report = {
        **suite_run.score.as_dict(),
        "run_id": run_id,
        "snapshot_id": manifest.snapshot_id,
        "model_id": model_id,
    }
    path = out / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "INVARIANT_TAG",
    "Manifest",
    "Pipeline",
    "SuiteRun",
    "SuiteSpec",
    "run_suite",
    "select",
    "write_report",
]
