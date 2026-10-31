"""WP-M03: the python-postgres pack's knowledge, proven on its own lab scenarios.

An oracle that classifies correctly (and plans nothing) leaves only the pack's
knowledge under test: runbook order, checks and thresholds must turn each
scenario's world into the right verdict. This runs on every PR without a model;
the live lab run measures the model on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from supportagent.adapters.files.pack_loader import load_pack_set
from supportagent.bootstrap import LabPipeline, build_rca_knowledge, load_lab
from supportagent.evals.lab import LabScenario, run_lab, select_split
from supportagent.ports.llm import ProviderRequest, ProviderResponse
from tests.unit.test_lab import _config

_REPO = Path(__file__).resolve().parents[2]
_PACK = "python-postgres"


def _scenarios() -> list[LabScenario]:
    scenarios, _, _ = load_lab(_config(), _PACK)
    return scenarios


class _Oracle:
    """Classifies each scenario as its truth says; proposes no plan; entailment supports."""

    def __init__(self, scenarios: list[LabScenario]) -> None:
        self._by_subject = {s.incident.subject: s.truth for s in scenarios}

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if "triage classifier" in request.system:
            truth = next(t for subj, t in self._by_subject.items() if subj in request.user)
            body: dict[str, Any] = {
                "classification": truth.classification,
                "severity": truth.severity,
                "summary": "s",
                "confidence": 0.9,
            }
        elif "root-cause investigation" in request.system:
            body = {"hypotheses": []}
        else:
            body = {"verdict": "supports"}
        return ProviderResponse(json.dumps(body), request.model_id, 1, 1, 1)


def test_pack_python_postgres_validates() -> None:
    packs = load_pack_set(
        taxonomy=_REPO / "taxonomy" / "classification.yaml",
        hypotheses=_REPO / "taxonomy" / "hypotheses.yaml",
        runbooks=_REPO / "runbooks",
        packs_dir=_REPO / "packs",
    )
    pack = packs.get(_PACK)
    assert len(pack.labels) == 3 and len(pack.hypotheses) == 13 and len(pack.runbook_ids) == 10
    knowledge = build_rca_knowledge(_config(), registered=_all_tools())
    assert {r.id for r, _ in knowledge.runbooks} >= set(pack.runbook_ids)


def _all_tools() -> frozenset[str]:
    from supportagent.bootstrap import KNOWN_TOOL_SCHEMAS  # noqa: PLC0415

    return frozenset(KNOWN_TOOL_SCHEMAS)


def test_every_pack_hypothesis_has_a_check() -> None:
    knowledge = build_rca_knowledge(_config(), registered=_all_tools())
    pack = load_pack_set(
        taxonomy=_REPO / "taxonomy" / "classification.yaml",
        hypotheses=_REPO / "taxonomy" / "hypotheses.yaml",
        runbooks=_REPO / "runbooks",
        packs_dir=_REPO / "packs",
    ).get(_PACK)
    in_runbooks = {s.hypothesis for r, _ in knowledge.runbooks for s in r.steps}
    for hypothesis in pack.hypotheses:
        assert hypothesis.id in knowledge.probes or hypothesis.id in in_runbooks, hypothesis.id


def test_lab_scenarios_cover_every_pack_hypothesis() -> None:
    pack = load_pack_set(
        taxonomy=_REPO / "taxonomy" / "classification.yaml",
        hypotheses=_REPO / "taxonomy" / "hypotheses.yaml",
        runbooks=_REPO / "runbooks",
        packs_dir=_REPO / "packs",
    ).get(_PACK)
    causes = {s.truth.root_cause for s in _scenarios()}
    missing = {h.id for h in pack.hypotheses} - causes
    assert not missing, missing
    assert len(_scenarios()) == 24  # 15 WP-M03, 4 docs WP-M08, disk full, Loki, 3 SOP ADR-0014
    controls = [s for s in _scenarios() if s.truth.root_cause is None]
    assert {s.truth.verdict for s in controls} == {"CONSISTENT_UNCONFIRMED", "INCONCLUSIVE"}


def test_holdout_split_disjoint() -> None:
    tune = select_split(_scenarios(), "tune")
    holdout = select_split(_scenarios(), "holdout")
    assert len(holdout) == 6
    assert not {s.id for s in tune} & {s.id for s in holdout}


def test_pack_knowledge_reaches_every_verdict_with_correct_classification() -> None:
    scenarios = _scenarios()
    pipeline = LabPipeline(_config(), provider=_Oracle(scenarios))
    report = run_lab(scenarios, pipeline, pack=_PACK, mode="oracle", model_id="oracle")
    failing = {s.scenario_id: s.failures for s in report.scores if not s.passed}
    assert not failing, failing
    assert report.metrics()["false_cause"] == {"count": 0, "rate": 0.0}
