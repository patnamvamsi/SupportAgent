"""WP-O6a: the oracle, java-spring and kubernetes plugins' packs, proven on their own labs.

As for python-postgres (``test_python_postgres_pack``), an oracle that
classifies correctly and plans nothing leaves only each pack's knowledge
under test: runbook order, checks and thresholds. The live lab run measures
the model on top.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from supportagent.adapters.files.pack_loader import load_pack_set
from supportagent.app.rca import RcaCase
from supportagent.bootstrap import (
    KNOWN_TOOL_SCHEMAS,
    LabPipeline,
    build_rca_knowledge,
    load_lab,
    load_plugins,
)
from supportagent.domain.model.catalog import ServiceEntry
from supportagent.domain.packs import PackSet
from supportagent.evals.lab import LabScenario, run_lab
from tests.unit.test_lab import _config
from tests.unit.test_python_postgres_pack import _Oracle

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_PACKS = ("oracle", "java-spring", "kubernetes", "nodejs", "mongodb", "rabbitmq")


def _pack_set() -> PackSet:
    return load_pack_set(
        taxonomy=_REPO / "taxonomy" / "classification.yaml",
        hypotheses=_REPO / "taxonomy" / "hypotheses.yaml",
        runbooks=_REPO / "runbooks",
        packs_dir=_REPO / "packs",
    )


def _scenarios(pack: str) -> list[LabScenario]:
    scenarios, _, _ = load_lab(_config(), pack)
    return scenarios


@pytest.mark.parametrize("pack", _PACKS)
def test_pack_knowledge_reaches_every_verdict(pack: str) -> None:
    scenarios = _scenarios(pack)
    pipeline = LabPipeline(_config(), provider=_Oracle(scenarios))
    report = run_lab(scenarios, pipeline, pack=pack, mode="oracle", model_id="oracle")
    failing = {s.scenario_id: s.failures for s in report.scores if not s.passed}
    assert not failing, failing
    assert report.metrics()["false_cause"] == {"count": 0, "rate": 0.0}
    assert any(s.truth.root_cause is None for s in scenarios)  # each lab has a no-cause control
    assert any(s.split == "holdout" for s in scenarios)


@pytest.mark.parametrize("pack", _PACKS)
def test_every_plugin_cause_has_a_tested_hypothesis(pack: str) -> None:
    """A cause the incident map can name is one this pack can test (WP-O6 readiness)."""
    knowledge = build_rca_knowledge(_config(), registered=KNOWN_TOOL_SCHEMAS)
    hypotheses = {h.id for h in _pack_set().get(pack).hypotheses}
    manifest = next(p.manifest for p in load_plugins() if p.manifest.id == pack)
    assert manifest.provides.pack == pack and manifest.status == "evaluated"
    for cause in manifest.provides.causes:
        assert cause.hypothesis in hypotheses, cause.id
    for hypothesis in hypotheses:
        assert hypothesis in knowledge.probes, hypothesis
    in_labs = {s.truth.root_cause for s in _scenarios(pack)}
    in_runbooks = {
        s.hypothesis
        for r, _ in knowledge.runbooks
        if r.id in _pack_set().get(pack).runbook_ids
        for s in r.steps
    }
    assert in_runbooks & hypotheses <= in_labs, "a runbook hypothesis no scenario proves"


def _case(stack: list[str], label: str) -> RcaCase:
    return RcaCase(
        id="c",
        tenant_id="t",
        trace_id="tr",
        service=ServiceEntry(name="ledger-api", stack=stack, databases=["ledgerdb"]),
        classification=label,
        severity="P2",
        origin="alert",
        opened_ts=0,
        summary="s",
    )


def _plan(stack: list[str], label: str) -> tuple[str, list[str]]:
    kb = build_rca_knowledge(_config(), registered=KNOWN_TOOL_SCHEMAS)
    matched = kb.for_case(_case(stack, label)).match(_case(stack, label))
    assert matched is not None
    runbook, checks = matched
    assert set(checks) == {s.id for s in runbook.steps}
    return runbook.id, [s.hypothesis for s in runbook.steps]


def test_stack_runbooks_merge_database_first() -> None:
    """A Spring service on Oracle: the lock check runs before the pool check it explains."""
    rid, order = _plan(["java", "spring-boot", "oracle"], "database/saturation")
    assert rid == "runbook:oracle-saturation+runbook:jvm-saturation"
    assert order.index("oracle_lock_contention") < order.index("jvm_connection_pool_exhaustion")
    rid, order = _plan(["java", "spring-boot"], "database/saturation")
    assert rid == "runbook:jvm-saturation"  # one stack pack: its runbook, not common's
    rid, _ = _plan(["golang"], "database/saturation")
    assert rid == "runbook:db-pool"  # no stack pack: common's


def test_merged_step_ids_stay_unique() -> None:
    from supportagent.app.rca import _merged  # noqa: PLC0415

    kb = build_rca_knowledge(_config(), registered=KNOWN_TOOL_SCHEMAS)
    by_id = {r.id: (r, c) for r, c in kb.runbooks}
    one = by_id["runbook:oracle-latency"]
    runbook, checks = _merged([one, one])
    ids = [s.id for s in runbook.steps]
    assert len(ids) == len(set(ids)) == 2 * len(one[0].steps)
    assert set(checks) == set(ids)


def test_lab_worlds_match_the_tool_schemas() -> None:
    """Every world entry names a known tool (``load_scenarios`` checks the shapes)."""
    for pack in _PACKS:
        worlds: list[Mapping[str, object]] = [s.world for s in _scenarios(pack)]
        assert all(set(w) <= set(KNOWN_TOOL_SCHEMAS) for w in worlds)
