"""WP-O6b (ADR-0015 §5): readiness from the system's own history.

The end-to-end tests replay each reference system's history with a model
stand-in that classifies every ticket as the truth says, so only the packs'
knowledge and the captured evidence are under test (as in
``test_plugin_packs``). The live run in the onboarding lab measures the
triage model on top.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supportagent.bootstrap import (
    ONBOARDING_DIR,
    LabPipeline,
    OnboardingRun,
    _past_incidents,
    build_readiness,
    build_system_graph,
    load_evidence,
    load_plugins,
    load_reference_system,
    readiness_testable,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.errors import ConfigError
from supportagent.domain.graph import Edge, Node, SystemGraph
from supportagent.domain.incident_map import GroundedReading, IncidentMap, build_map
from supportagent.domain.model.readiness import ReadinessGates
from supportagent.evals.lab import LabScore
from supportagent.evals.readiness import (
    ReadinessReport,
    Replayed,
    assess,
    history_scenarios,
    regressions,
    service_entry,
)
from supportagent.ports.llm import ProviderRequest, ProviderResponse

pytestmark = pytest.mark.unit

_LAB = Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml"


def _truth_map(system: str) -> IncidentMap:
    """The incident map a perfect onboarding would build: each ticket read as its truth class."""
    d = ONBOARDING_DIR / system
    truth = {n: c for c in load_reference_system(d).truth.incident_classes for n in c.incidents}
    past = _past_incidents(d)
    readings = {
        i.number: GroundedReading(i.number, truth[i.number].labels[0], truth[i.number].cause, "q")
        for i in past
    }
    return build_map(past, readings)


class _Classifier:
    """Classifies each ticket as its truth class's first label; plans nothing; entails."""

    def __init__(self, system: str) -> None:
        d = ONBOARDING_DIR / system
        truth = {n: c for c in load_reference_system(d).truth.incident_classes for n in c.incidents}
        self._labels = {i.title: truth[i.number].labels[0] for i in _past_incidents(d)}

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if "triage classifier" in request.system:
            label = next(lab for title, lab in self._labels.items() if title in request.user)
            body: dict[str, Any] = {
                "classification": label,
                "severity": "P2",
                "summary": "s",
                "confidence": 0.9,
            }
        elif "root-cause investigation" in request.system:
            body = {"hypotheses": []}
        else:
            body = {"verdict": "supports"}
        return ProviderResponse(json.dumps(body), request.model_id, 1, 1, 1)


def _report(system: str) -> ReadinessReport:
    d = ONBOARDING_DIR / system
    cfg = load_config_offline(_LAB)
    run = OnboardingRun(
        None,  # type: ignore[arg-type] # the result and plan aren't read
        [],
        build_system_graph(d, None),
        _truth_map(system),
        None,  # type: ignore[arg-type]
    )
    return build_readiness(d, run, LabPipeline(cfg, provider=_Classifier(system)), cfg)


def test_the_graph_gives_each_service_its_stack_databases_and_namespace() -> None:
    graph = build_system_graph(ONBOARDING_DIR / "system-a", None).graph
    entry = service_entry(graph, "payment-service")
    assert entry["stack"] == ["java", "kubernetes", "oracle", "spring-boot"]
    assert entry["databases"] == ["paydb"]
    assert (entry["k8s_namespace"], entry["k8s_selector"]) == ("payments", "app=payment-service")
    assert service_entry(graph, "payments-portal")["stack"] == ["angular"]
    assert service_entry(graph, "unknown-ci") == {"name": "unknown-ci", "stack": []}


def test_a_schema_the_service_reads_or_writes_names_its_database() -> None:
    """The graph keeps ``writes schema:paydb.settlement`` and drops ``uses database:paydb``."""
    graph = SystemGraph(
        nodes={
            "job:settlement-batch": Node("job:settlement-batch", "Job", {"java"}),
            "schema:paydb.settlement": Node("schema:paydb.settlement", "Datastore", {"oracle"}),
            "database:paydb": Node("database:paydb", "Database", {"oracle"}),
        },
        edges={
            e.key: e
            for e in (
                Edge("job:settlement-batch", "writes", "schema:paydb.settlement"),
                Edge("schema:paydb.settlement", "part_of", "database:paydb"),
            )
        },
    )
    entry = service_entry(graph, "settlement-batch")
    assert entry["databases"] == ["paydb"] and entry["stack"] == ["java", "oracle"]


def test_a_queue_the_service_uses_names_its_broker() -> None:
    """The graph keeps ``consumes topic:events`` and drops ``uses runtime:rabbitmq/mq``."""
    nodes = [
        Node("service:worker", "Service", {"nodejs"}),
        Node("topic:events", "Topic", {"rabbitmq"}),
        Node("runtime:rabbitmq/mq", "Runtime", {"rabbitmq", "erlang"}),
        Node("service:billing", "Service", {"java"}),
    ]
    edges = [
        Edge("service:worker", "consumes", "topic:events"),
        Edge("topic:events", "part_of", "runtime:rabbitmq/mq"),
        Edge("service:worker", "calls", "service:billing"),
    ]
    graph = SystemGraph(nodes={n.id: n for n in nodes}, edges={e.key: e for e in edges})
    entry = service_entry(graph, "worker")
    assert entry["stack"] == ["erlang", "nodejs", "rabbitmq"]  # not java: a call isn't a stack
    assert "databases" not in entry


def test_history_becomes_scenarios_and_incidents_without_evidence_are_counted() -> None:
    d = ONBOARDING_DIR / "system-a"
    imap = _truth_map("system-a")
    graph = build_system_graph(d, None).graph
    evidence = load_evidence(d)
    scenarios, missing = history_scenarios(
        imap, _past_incidents(d), graph, evidence, system="payments-platform"
    )
    by_class = {c.id: c for c in imap.classes}
    noise = next(c for c in imap.classes if c.cause is None)
    replayed = {s.incident for s in scenarios}
    assert set(noise.incidents) <= replayed  # abstaining needs no evidence
    for class_id, numbers in missing.items():
        assert by_class[class_id].cause is not None or not numbers
        assert not set(numbers) & replayed and all(n not in evidence for n in numbers)
    one = next(s for s in scenarios if s.class_id == "oracle-lock-contention").scenario
    assert one.truth.root_cause == "oracle_lock_contention" and one.service["databases"] == [
        "paydb"
    ]
    assert one.incident.origin == "ticket" and "oracle.locks" in one.world


def test_evidence_naming_an_unknown_tool_does_not_load(tmp_path: Path) -> None:
    (tmp_path / "sources" / "evidence").mkdir(parents=True)
    (tmp_path / "sources" / "evidence" / "INC1.yaml").write_text(
        "incident: INC1\nworld:\n  oracle.write: {result: {}}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unknown tool"):
        load_evidence(tmp_path)
    (tmp_path / "sources" / "evidence" / "INC1.yaml").write_text(
        "incident: INC1\nworld:\n  oracle.locks: {result: {database: paydb}}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="misses"):
        load_evidence(tmp_path)


def test_a_cause_is_testable_only_with_evaluated_knowledge() -> None:
    why_not = readiness_testable(load_config_offline(_LAB), load_plugins())
    assert why_not("oracle/lock_contention") is None
    assert why_not("recent_change_push") is None  # built in: the common and kubernetes packs
    assert "angular plugin is a skeleton" in (why_not("ui/cors_or_stale_bundle") or "")
    assert "no probe or runbook" in (why_not("made/up") or "")


def _score(*, ok: bool, false: bool = False) -> LabScore:
    return LabScore(
        scenario_id="s",
        split="tune",
        classification_ok=True,
        severity_ok=True,
        verdict_ok=ok,
        root_cause_ok=ok,
        false_cause=false,
        trap_hit=False,
        citations_ok=True,
        abstained_correctly=None,
        tool_calls=1,
        tokens=0,
        failures=(),
    )


def test_each_gate_keeps_a_class_from_being_ready() -> None:
    imap = _truth_map("system-a")
    shares = {c.id: c.share for c in imap.classes}
    replayed = {
        # ready: 3 of 3
        "oracle-tablespace-full": [
            Replayed(f"I{i}", "oracle_tablespace_full", _score(ok=True)) for i in range(3)
        ],
        # a single false cause
        "oracle-lock-contention": [
            *(Replayed(f"L{i}", "oracle_lock_contention", _score(ok=True)) for i in range(4)),
            Replayed("L9", "jvm_connection_pool_exhaustion", _score(ok=False, false=True)),
        ],
        # too few with evidence
        "oracle-plan-regression": [Replayed("P1", "oracle_plan_regression", _score(ok=True))],
        # top-1 below the gate
        "jvm-heap-exhaustion": [
            Replayed("H1", "jvm_heap_exhaustion", _score(ok=True)),
            *(Replayed(f"H{i}", None, _score(ok=False)) for i in range(2, 5)),
        ],
    }
    why_not = readiness_testable(load_config_offline(_LAB), load_plugins())
    report = assess(
        imap,
        replayed,
        {"oracle-plan-regression": ("P2", "P3")},
        testable=why_not,
        gates=ReadinessGates(),
        system="payments-platform",
        model_id="m",
    )
    by = {c.class_id: c for c in report.classes}
    assert by["oracle-tablespace-full"].status == "ready"
    lock = by["oracle-lock-contention"]
    assert lock.status == "not_ready" and lock.false_causes == (
        ("L9", "jvm_connection_pool_exhaustion"),
    )
    assert (
        "1 scenario(s) with evidence; needs 3 (2 without evidence)"
        in by["oracle-plan-regression"].reasons
    )
    assert by["jvm-heap-exhaustion"].reasons == ("found the cause below the 75% needed",)
    assert by["ui-cors-or-stale-bundle"].status == "untestable"
    assert report.ready_share == round(shares["oracle-tablespace-full"], 3)
    assert not report.ready and report.false_causes == 1
    assert [c.share for c in report.classes] == sorted(
        (c.share for c in report.classes), reverse=True
    )
    assert regressions(
        report, {"oracle-lock-contention": "ready", "oracle-tablespace-full": "ready"}
    ) == ["oracle-lock-contention"]
    as_json = report.as_dict()
    assert as_json["ready"] is False and as_json["not_measured"] == ["shadow replay"]


def test_system_a_knowledge_and_evidence_make_most_of_its_history_ready() -> None:
    report = _report("system-a")
    status = report.statuses()
    assert report.false_causes == 0
    for ready in (
        "oracle-lock-contention",
        "oracle-tablespace-full",
        "oracle-plan-regression",
        "jvm-heap-exhaustion",
        "recent-change-push",
        "upstream-provider",
        "request-access",
    ):
        assert status[ready] == "ready", (ready, report.lines())
    assert status["ui-cors-or-stale-bundle"] == "untestable"
    # Half its tickets are logged on the gateway, which only sees its dependency's 503s.
    pool = next(c for c in report.classes if c.class_id == "jvm-connection-pool-exhaustion")
    assert pool.status == "not_ready" and pool.correct < pool.scenarios
    assert report.ready and report.ready_share >= report.bar


def test_system_b_has_no_evaluated_knowledge_yet() -> None:
    report = _report("system-b")
    testable = [c for c in report.classes if c.status != "untestable"]
    assert [c.cause for c in testable] == [None]  # only the no-cause class: it abstains
    assert not report.ready and report.false_causes == 0
    assert all("skeleton" in c.reasons[0] for c in report.classes if c.status == "untestable")


def test_system_c_is_ready_from_plugins_that_are_only_data() -> None:
    """ADR-0016: Node.js, MongoDB and RabbitMQ knowledge with no core code proves most classes."""
    report = _report("system-c")
    status = report.statuses()
    assert report.false_causes == 0
    for ready in (
        "mongodb-missing-index",
        "mongodb-connection-pool-exhausted",
        "rabbitmq-queue-backlog",
        "rabbitmq-memory-alarm",
        "node-heap-oom",
        "node-event-loop-blocked",
        "recent-change-push",
    ):
        assert status[ready] == "ready", (ready, report.lines())
    # Three of the five carrier outages left no evidence: too few to judge.
    assert status["upstream-provider"] == "not_ready"
    assert report.ready
