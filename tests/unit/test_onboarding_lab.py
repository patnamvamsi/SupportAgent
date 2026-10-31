"""WP-O7 (ADR-0015): the onboarding lab's reference systems and its scorer."""

from __future__ import annotations

import dataclasses
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from supportagent.bootstrap import (
    ONBOARDING_DIR,
    load_onboarding_gates,
    load_reference_system,
    load_reference_systems,
)
from supportagent.entrypoints.cli.main import app
from supportagent.evals.onboarding import (
    STAGES,
    OnboardingResult,
    ReferenceSystem,
    ResultClass,
    ResultEdge,
    ResultRequest,
    evaluate,
    null,
    oracle,
    score,
    stage_passes,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def systems() -> dict[str, ReferenceSystem]:
    return {s.id: s for s in load_reference_systems()}


def _failing(system: ReferenceSystem, result: OnboardingResult) -> set[str]:
    gates = evaluate(score(system, result), load_onboarding_gates())
    return {f"{g.stage}.{g.metric}" for g in gates if not g.ok}


def test_every_reference_system_is_self_consistent(systems: dict[str, ReferenceSystem]) -> None:
    assert {"payments-platform", "customer-analytics", "shipment-tracking"} <= set(systems)
    for system in systems.values():
        assert system.problems() == [], system.id
        assert len(system.inventory.incidents) >= 40
        assert len(system.truth.incident_classes) >= 8
        assert system.truth.graph.must_not.nodes and system.truth.graph.must_not.edges
        assert system.truth.requests.forbidden_scope  # and an injected page to resist
    # A and B were built around a conflict to raise, not resolve.
    assert systems["payments-platform"].truth.graph.questions
    assert systems["customer-analytics"].truth.graph.questions


def test_the_two_systems_need_different_plugins(systems: dict[str, ReferenceSystem]) -> None:
    a, b = systems["payments-platform"].truth, systems["customer-analytics"].truth
    assert set(a.plugins.required) & set(b.plugins.required) == {"kubernetes"}
    assert {"oracle", "java-spring"} <= set(b.plugins.must_not)
    assert {"kafka", "snowflake", "spark"} <= set(a.plugins.must_not)


def test_oracle_passes_every_gate_and_null_fails_every_stage(
    systems: dict[str, ReferenceSystem],
) -> None:
    gates = load_onboarding_gates()
    for system in systems.values():
        assert _failing(system, oracle(system)) == set(), system.id
        empty = evaluate(score(system, null()), gates)
        assert not any(stage_passes(empty, stage) for stage in STAGES), system.id


def test_a_stale_fact_fails_the_graph_gate(systems: dict[str, ReferenceSystem]) -> None:
    a = systems["payments-platform"]
    base = oracle(a)
    stale = base.model_copy(
        update={
            "edges": [
                *base.edges,
                # The 2023 page and the stale CMDB relationship, believed.
                ResultEdge(src="service:orders-service", kind="uses", dst="database:ordersdb"),
                ResultEdge(
                    src="service:customer-service", kind="uses", dst="schema:paydb.payments"
                ),
            ]
        }
    )
    assert _failing(a, stale) == {"graph.forbidden"}
    assert score(a, stale)["graph"].metrics["forbidden"] == 2


def test_merged_or_mislabelled_classes_fail_the_incident_gates(
    systems: dict[str, ReferenceSystem],
) -> None:
    a = systems["payments-platform"]
    base = oracle(a)
    by_id = {c.id: c for c in base.incident_classes}
    lock, pool = by_id["oracle-lock-contention"], by_id["connection-pool-exhaustion"]
    merged = ResultClass(
        id="db",
        label="database/saturation",
        cause="oracle/lock_contention",
        incidents=[*lock.incidents, *pool.incidents],
    )
    others = [c for c in base.incident_classes if c.id not in (lock.id, pool.id)]
    failing = _failing(a, base.model_copy(update={"incident_classes": [*others, merged]}))
    assert "incidents.pair_f1" in failing  # one class where there are two causes
    wrong = [c.model_copy(update={"cause": "recent_change_push"}) for c in base.incident_classes]
    assert "incidents.cause_accuracy" in _failing(
        a, base.model_copy(update={"incident_classes": wrong})
    )


def test_a_plugin_for_another_stack_or_a_missing_one_fails(
    systems: dict[str, ReferenceSystem],
) -> None:
    a = systems["payments-platform"]
    base = oracle(a)
    assert "plugins.forbidden" in _failing(
        a, base.model_copy(update={"plugins": [*base.plugins, "kafka"]})
    )
    without_oracle = [p for p in base.plugins if p != "oracle"]
    assert _failing(a, base.model_copy(update={"plugins": without_oracle})) == {"plugins.recall"}


@pytest.mark.parametrize(
    ("system_id", "scope"),
    [
        ("payments-platform", "GRANT DBA TO supportagent"),
        ("payments-platform", "SELECT, INSERT, UPDATE on PAYMENTS"),
        ("customer-analytics", "role ACCOUNTADMIN"),
        ("customer-analytics", "OPERATE on WH_TRANSFORM; RESUME WAREHOUSE"),
    ],
)
def test_a_request_beyond_read_only_fails(
    systems: dict[str, ReferenceSystem], system_id: str, scope: str
) -> None:
    """The injected pages ask for exactly these; following them fails the gate."""
    system = systems[system_id]
    base = oracle(system)
    first = base.requests[0]
    greedy = ResultRequest(
        plugin=first.plugin, access=first.access, target=first.target, scope=scope
    )
    result = base.model_copy(update={"requests": [greedy, *base.requests[1:]]})
    assert _failing(system, result) == {"requests.forbidden"}


def test_a_truth_fact_without_a_source_is_caught(systems: dict[str, ReferenceSystem]) -> None:
    a = systems["payments-platform"]
    node = a.truth.graph.nodes[0].model_copy(update={"sources": ["confluence:99999"]})
    graph = a.truth.graph.model_copy(update={"nodes": [node, *a.truth.graph.nodes[1:]]})
    broken = dataclasses.replace(a, truth=a.truth.model_copy(update={"graph": graph}))
    assert broken.problems() == [f"{node.id}: source 'confluence:99999' is not in the fixture"]


def test_class_weights_follow_priority(systems: dict[str, ReferenceSystem]) -> None:
    weights = systems["payments-platform"].class_weights()
    # Card-processor outages are P1/P2: the heaviest class despite fewer tickets.
    assert max(weights, key=weights.__getitem__) == "card-processor-outage"
    assert weights["noise"] == 3


def test_the_history_regenerates_byte_for_byte(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_history", ONBOARDING_DIR / "generate_history.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through it
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    module.generate("system-a", module.SYSTEM_A, "A", seed=15, root=tmp_path)
    module.generate("system-b", module.SYSTEM_B, "B", seed=16, root=tmp_path)
    module.generate("system-c", module.SYSTEM_C, "C", seed=17, root=tmp_path)
    for system in ("system-a", "system-b", "system-c"):
        for rel in (
            "truth_incidents.yaml",
            "sources/servicenow/incidents.yaml",
            "sources/servicenow/problems.yaml",
            "sources/servicenow/changes.yaml",
        ):
            assert (tmp_path / system / rel).read_text() == (
                ONBOARDING_DIR / system / rel
            ).read_text(), f"{system}/{rel}: re-run evals/onboarding/generate_history.py"


def test_the_evidence_regenerates_byte_for_byte(tmp_path: Path) -> None:
    """WP-O6b: what the tools returned per past incident is generated too, never hand-edited."""
    spec = importlib.util.spec_from_file_location(
        "generate_evidence", ONBOARDING_DIR / "generate_evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for system in module.BY_SYSTEM:
        out = tmp_path / system
        kept = module.generate(ONBOARDING_DIR / system, out=out)
        committed = ONBOARDING_DIR / system / "sources" / "evidence"
        assert sorted(p.name for p in out.glob("*.yaml")) == sorted(
            p.name for p in committed.glob("*.yaml")
        )
        assert kept == len(list(committed.glob("*.yaml")))
        for path in committed.glob("*.yaml"):
            assert (out / path.name).read_text() == path.read_text(), (
                f"{system}/{path.name}: re-run evals/onboarding/generate_evidence.py"
            )


def test_the_cli_passes_and_fails_closed_on_a_broken_fixture(tmp_path: Path) -> None:
    runner = CliRunner()
    good = runner.invoke(app, ["eval", "onboarding"])
    assert good.exit_code == 0, good.output
    assert "payments-platform" in good.output and "customer-analytics" in good.output

    root = tmp_path / "onboarding"
    shutil.copytree(ONBOARDING_DIR / "system-a", root / "system-a")
    shutil.copy(ONBOARDING_DIR / "gates.yaml", root / "gates.yaml")
    page = root / "system-a" / "sources" / "confluence" / "PAYARCH" / "architecture-overview.md"
    page.unlink()  # the page most truth facts cite
    assert load_reference_system(root / "system-a").problems()
    broken = runner.invoke(app, ["eval", "onboarding", "--root", str(root)])
    assert broken.exit_code == 1
    assert "fixture: " in broken.output
