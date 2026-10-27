"""WP-M02: the incident lab: scenarios, world-backed tools, scoring, gates, replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.files.eval_loader import load_yaml_document
from supportagent.adapters.llm.client import CachingLLMProvider
from supportagent.bootstrap import (
    KNOWN_TOOL_SCHEMAS,
    LabPipeline,
    build_lab_pipeline,
    load_lab,
)
from supportagent.domain.errors import ConfigError, ToolError
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.trace import TraceContext
from supportagent.evals.lab import (
    LabGates,
    LabOutcome,
    LabScenario,
    WorldEntry,
    WorldTools,
    baseline_of,
    load_scenarios,
    run_lab,
    score_outcome,
    select_split,
)
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse
from tests.unit.test_triage_service import _shadow_config

_REPO = Path(__file__).resolve().parents[2]
_PLANS = {"nightly-sync": ["dependency_failure", "recent_change_push"]}


class _ScriptedModel:
    """A provider that answers like a competent model: by slot, from the case."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        system, user = request.system, request.user
        if "triage classifier" in system:
            label = "batch/failure" if "nightly" in user else "database/saturation"
            body: dict[str, Any] = {
                "classification": label,
                "severity": "P3",
                "summary": "s",
                "confidence": 0.9,
            }
        elif "root-cause investigation" in system:
            service = next((k for k in _PLANS if k in user), None)
            body = {"hypotheses": _PLANS.get(service or "", [])}
        else:
            body = {"verdict": "supports"}
        return ProviderResponse(
            raw_text=json.dumps(body),
            resolved_model=request.model_id,
            tokens_in=10,
            tokens_out=5,
            latency_ms=1,
        )


def _config() -> Any:
    base = _shadow_config()
    paths = base.paths.model_copy(
        update={
            "packs": str(_REPO / "packs"),
            "catalog": str(_REPO / "catalog" / "services.example.yaml"),
            "gates": str(_REPO / "evals" / "gates.yaml"),
            "runbooks": str(_REPO / "runbooks"),
        }
    )
    return base.model_copy(update={"paths": paths})


def _scenarios() -> list[LabScenario]:
    scenarios, _, _ = load_lab(_config(), "common")
    return scenarios


def _scenario(**overrides: Any) -> LabScenario:
    base: dict[str, Any] = {
        "id": "pool-test",
        "pack": "common",
        "description": "d",
        "incident": {"subject": "core-db pool full"},
        "service": {"name": "core-db", "stack": ["postgres"]},
        "truth": {
            "classification": "database/saturation",
            "severity": "P3",
            "root_cause": "capacity_decay",
            "verdict": "SUPPORTED",
            "must_cite": ["logs.grep"],
            "must_not_claim": ["recent_change_push"],
        },
        "why": "w",
    }
    base.update(overrides)
    return LabScenario.model_validate(base)


def _outcome(**overrides: Any) -> LabOutcome:
    base: dict[str, Any] = {
        "scenario_id": "pool-test",
        "classification": "database/saturation",
        "severity": "P3",
        "preclass_label": None,
        "verdict": "SUPPORTED",
        "supported": "capacity_decay",
        "tested": ("recent_change_push", "capacity_decay"),
        "cited_tools": frozenset({"logs.grep"}),
        "tool_calls": 2,
        "tokens": 100,
    }
    base.update(overrides)
    return LabOutcome(**base)


# --- the schema ---------------------------------------------------------------------


def test_scenario_schema_requires_truth() -> None:
    with pytest.raises(ValueError, match="truth"):
        LabScenario.model_validate(
            {
                "id": "no-truth",
                "pack": "common",
                "description": "d",
                "incident": {"subject": "s"},
                "service": {"name": "core-db"},
                "why": "w",
            }
        )
    with pytest.raises(ValueError, match="root_cause"):
        _scenario(
            truth={
                "classification": "a/b",
                "severity": "P3",
                "root_cause": None,
                "verdict": "SUPPORTED",
            }
        )


def test_world_fixture_served_per_capability() -> None:
    world = {
        "logs.grep": WorldEntry(result={"path": "p", "count": 1, "matches": []}),
        "gha.recent_runs": WorldEntry(error="GitHub said 503"),
    }
    tools = WorldTools(world)
    service = ServiceEntry(name="core-db")
    trace = TraceContext("acme", "tr", "s")
    assert tools.call("logs.grep", {}, service, trace).data["count"] == 1
    with pytest.raises(ToolError, match="503"):
        tools.call("gha.recent_runs", {}, service, trace)
    with pytest.raises(ToolError, match="not available"):
        tools.call("airflow.dag_runs", {}, service, trace)
    assert tools.calls == ["logs.grep", "gha.recent_runs", "airflow.dag_runs"]


def test_world_result_must_match_its_tool_schema(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "id: bad-world\npack: common\ndescription: d\nincident: {subject: s}\n"
        "service: {name: core-db}\nwhy: w\n"
        "world: {logs.grep: {result: {path: p}}}\n"
        "truth: {classification: a/b, severity: P3, root_cause: null, verdict: INCONCLUSIVE}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="misses"):
        load_scenarios(
            tmp_path, pack="common", tool_schemas=KNOWN_TOOL_SCHEMAS, read_yaml=load_yaml_document
        )


# --- scoring ------------------------------------------------------------------------


def test_false_cause_counts_supported_on_wrong_hypothesis() -> None:
    score = score_outcome(_scenario(), _outcome(supported="recent_change_push"))
    assert score.false_cause and score.trap_hit and not score.passed
    assert any("FALSE CAUSE" in f for f in score.failures)


def test_inconclusive_correct_when_no_cause_findable() -> None:
    scenario = _scenario(
        truth={
            "classification": "database/saturation",
            "severity": "P3",
            "root_cause": None,
            "verdict": "INCONCLUSIVE",
        }
    )
    good = score_outcome(scenario, _outcome(verdict="INCONCLUSIVE", supported=None))
    assert good.passed and good.abstained_correctly
    bad = score_outcome(scenario, _outcome())
    assert bad.false_cause and not bad.passed, "claiming any cause where none exists"


def test_supported_without_the_required_citation_fails() -> None:
    score = score_outcome(_scenario(), _outcome(cited_tools=frozenset({"gha.recent_runs"})))
    assert not score.citations_ok and not score.passed


# --- gates ---------------------------------------------------------------------------


class _Fixed:
    def __init__(self, outcomes: dict[str, LabOutcome]) -> None:
        self._outcomes = outcomes

    def run(self, scenario: LabScenario, sample: int) -> LabOutcome:
        return self._outcomes[scenario.id]


def test_lab_gate_blocks_regression_below_band() -> None:
    scenario = _scenario()
    pipeline = _Fixed({"pool-test": _outcome(verdict="INCONCLUSIVE", supported=None)})
    report = run_lab(
        [scenario], pipeline, pack="common", mode="t", model_id="m", baseline={"pool-test": True}
    )
    assert report.regressions == ["pool-test"]
    assert not report.passed
    assert {g.name: g.ok for g in report.gates}["no_regressions"] is False


def test_any_false_cause_fails_the_gate() -> None:
    report = run_lab(
        [_scenario()],
        _Fixed({"pool-test": _outcome(supported="recent_change_push")}),
        pack="common",
        mode="t",
        model_id="m",
        gates=LabGates(),
    )
    assert {g.name: g.ok for g in report.gates}["false_cause_rate"] is False


def test_false_cause_in_any_sample_counts() -> None:
    outcomes = iter([_outcome(), _outcome(supported="recent_change_push"), _outcome()])

    class _Varying:
        def run(self, scenario: LabScenario, sample: int) -> LabOutcome:
            return next(outcomes)

    report = run_lab([_scenario()], _Varying(), pack="common", mode="t", model_id="m", samples=3)
    assert report.scores[0].false_cause, "one confident wrong answer in three is not safe"


def test_holdout_scenarios_excluded_from_tuning_runs() -> None:
    scenarios = _scenarios()
    tune = select_split(scenarios, "tune")
    holdout = select_split(scenarios, "holdout")
    assert holdout and all(s.split == "holdout" for s in holdout)
    assert not {s.id for s in tune} & {s.id for s in holdout}


# --- the shipped scenarios, end to end --------------------------------------------------


def test_common_scenarios_pass_with_a_competent_model() -> None:
    """The runbook path, the proposed-plan path and the no-cause control all score right."""
    model = _ScriptedModel()
    pipeline = LabPipeline(_config(), provider=model)
    report = run_lab(_scenarios(), pipeline, pack="common", mode="scripted", model_id="m")
    failing = {s.scenario_id: s.failures for s in report.scores if not s.passed}
    assert not failing, failing
    assert report.passed


def test_cached_replay_is_deterministic(tmp_path: Path) -> None:
    """Record once with a model, then replay with no model at all: same report, no misses."""
    config = _config()
    scenarios = select_split(_scenarios(), "tune")
    holder: list[CachingLLMProvider] = []

    def record(inner: LLMProvider) -> LLMProvider:
        holder.append(CachingLLMProvider(inner, directory=tmp_path, snapshot_id="lab-common"))
        return holder[-1]

    recorder = LabPipeline(config, provider=_ScriptedModel(), decorate=record)
    recorder.caching = holder[-1]
    recorded = run_lab(scenarios, recorder, pack="common", mode="live", model_id="m")
    assert recorder.caching.misses > 0, "the first run records"

    replay = build_lab_pipeline(config, recordings=tmp_path, live=False, pack_id="common")
    replayed = run_lab(scenarios, replay, pack="common", mode="replay", model_id="m")
    assert replay.caching is not None and replay.caching.misses == 0
    assert baseline_of(replayed) == baseline_of(recorded)
    assert [s.failures for s in replayed.scores] == [s.failures for s in recorded.scores]
