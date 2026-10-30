"""WP-O3 (ADR-0015 §3): the system graph: merge rules, prose grounding, structured extraction."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.bootstrap import ONBOARDING_DIR, build_system_graph, load_reference_system
from supportagent.domain.graph import AliasFact, EdgeFact, Fact, NodeFact, SystemGraph, build
from supportagent.domain.graph_prose import (
    GraphExtraction,
    ProseEntity,
    ProseFact,
    Rejected,
    chunks,
    ground,
)
from supportagent.evals.onboarding import OnboardingResult, ResultEdge, ResultNode, score

pytestmark = pytest.mark.unit


def _n(node_id: str, kind: str, source: str = "git:r/f", layer: int = 1) -> NodeFact:
    return NodeFact(node_id, kind, source, layer, "test")  # type: ignore[arg-type]


def _e(src: str, kind: str, dst: str, source: str = "git:r/f", layer: int = 1) -> EdgeFact:
    return EdgeFact(src, kind, dst, source, layer, "test")  # type: ignore[arg-type]


# --- merge rules -------------------------------------------------------------------------


def test_aliases_merge_two_names_for_one_entity() -> None:
    graph = build(
        [
            _n("job:task_build_daily_sales", "Job"),
            _n("job:daily-sales-task", "Job", "cmdb:ci-1"),
            AliasFact("job:task_build_daily_sales", "job:daily-sales-task", "cmdb:ci-1", 1, "t"),
            _n("table:raw.orders", "Datastore"),
            _e("job:task_build_daily_sales", "reads", "table:raw.orders"),
        ]
    )
    assert set(graph.nodes) == {"job:daily-sales-task", "table:raw.orders"}
    assert graph.nodes["job:daily-sales-task"].sources == {"git:r/f", "cmdb:ci-1"}
    assert set(graph.edges) == {("job:daily-sales-task", "reads", "table:raw.orders")}


def test_a_finer_edge_subsumes_a_coarse_one() -> None:
    graph = build(
        [
            _n("service:orders", "Service"),
            _n("database:paydb", "Database"),
            _n("schema:paydb.orders", "Datastore"),
            _e("schema:paydb.orders", "part_of", "database:paydb"),
            _e("service:orders", "uses", "database:paydb", "cmdb:rel-1"),
            _e("service:orders", "uses", "schema:paydb.orders", "confluence:1", layer=3),
        ]
    )
    assert ("service:orders", "uses", "database:paydb") not in graph.edges
    assert ("service:orders", "uses", "schema:paydb.orders") in graph.edges
    assert any("subsumed" in d for d in graph.dropped)


def test_configuration_overrules_a_stale_cmdb_edge_and_asks() -> None:
    graph = build(
        [
            _n("service:customers", "Service"),
            _n("database:paydb", "Database", "cmdb:ci-p"),
            _n("database:crmdb", "Database"),
            _e("service:customers", "uses", "database:paydb", "cmdb:rel-7"),
            _e("service:customers", "uses", "database:crmdb", "git:customers/application.yml"),
        ]
    )
    assert set(graph.edges) == {("service:customers", "uses", "database:crmdb")}
    [question] = graph.questions
    assert question.about == "service:customers uses which database"
    assert question.sources == ("cmdb:rel-7",)
    assert "database:crmdb" in question.detail


def test_an_edge_to_an_undescribed_entity_is_dropped_and_said_why() -> None:
    graph = build([_n("service:a", "Service"), _e("service:a", "calls", "service:ghost")])
    assert graph.edges == {}
    assert graph.dropped == ["service:a calls service:ghost: no fact describes an endpoint"]


def test_the_most_trusted_layer_names_a_nodes_kind() -> None:
    graph = build(
        [
            _n("job:settlement", "Service", "confluence:1", layer=3),
            _n("job:settlement", "Job", "git:s/pom.xml", layer=1),
        ]
    )
    assert graph.nodes["job:settlement"].kind == "Job"


# --- grounding prose -----------------------------------------------------------------------

_PAGE = """# Overview

| Component | What it does | Talks to |
|---|---|---|
| orders-service | Creates orders | Oracle `PAYDB` (schema ORDERS), payment-service |

settlement-batch writes **SETTLEMENT** in PAYDB every night.
"""
_KNOWN = {
    "service:orders-service",
    "service:payment-service",
    "schema:paydb.orders",
    "schema:paydb.settlement",
    "job:settlement-batch",
}


def _ground(
    *facts: ProseFact, entities: tuple[ProseEntity, ...] = ()
) -> tuple[list[Fact], list[Rejected]]:
    return ground(
        GraphExtraction(entities=entities, facts=facts),
        page_id="10001",
        page_text=_PAGE,
        known=_KNOWN,
    )


def test_a_grounded_fact_quotes_the_page_and_names_both_ends() -> None:
    row = "| orders-service | Creates orders | Oracle `PAYDB` (schema ORDERS), payment-service |"
    facts, rejected = _ground(
        ProseFact("service:orders-service", "uses", "schema:paydb.orders", row),
        ProseFact(
            "job:settlement-batch",
            "writes",
            "schema:paydb.settlement",
            "settlement-batch writes SETTLEMENT in PAYDB every night.",
        ),
    )
    assert rejected == []
    edges = [f for f in facts if isinstance(f, EdgeFact)]
    assert [(f.src, f.kind, f.dst, f.source, f.layer) for f in edges] == [
        ("service:orders-service", "uses", "schema:paydb.orders", "confluence:10001", 3),
        ("job:settlement-batch", "writes", "schema:paydb.settlement", "confluence:10001", 3),
    ]


@pytest.mark.parametrize(
    ("fact", "why"),
    [
        (
            ProseFact(
                "service:orders-service",
                "uses",
                "schema:paydb.orders",
                "orders-service stores orders in PAYDB.ORDERS",
            ),
            "not in the page",
        ),
        (
            ProseFact(
                "service:orders-service",
                "calls",
                "service:payment-service",
                "orders-service | Creates orders",
            ),
            "doesn't name service:payment-service",
        ),
        (
            ProseFact(
                "service:orders-service",
                "calls",
                "service:mystery",
                "orders-service | Creates orders",
            ),
            "unknown entity service:mystery",
        ),
        (
            ProseFact("service:orders-service", "owns", "schema:paydb.orders", "orders-service"),
            "not a fact kind",
        ),
        (ProseFact("service:orders-service", "uses", "schema:paydb.orders", ""), "no usable quote"),
    ],
)
def test_an_ungrounded_fact_is_rejected_with_its_reason(fact: ProseFact, why: str) -> None:
    facts, [rejected] = _ground(fact)
    assert facts == [] and why in rejected.why


def test_a_new_entity_needs_its_own_grounded_quote() -> None:
    facts, rejected = _ground(
        ProseFact(
            "job:settlement-batch",
            "writes",
            "database:paydb",
            "settlement-batch writes SETTLEMENT in PAYDB every night.",
        ),
        entities=(ProseEntity("database:paydb", "Database", "Oracle `PAYDB` (schema ORDERS)"),),
    )
    assert rejected == []
    assert {type(f).__name__ for f in facts} == {"NodeFact", "EdgeFact"}
    _, rejected = _ground(
        entities=(ProseEntity("database:cardb", "Database", "Oracle `PAYDB` (schema ORDERS)"),)
    )
    assert rejected[0].why == "the quote doesn't name it"


def test_long_pages_are_split_at_headings() -> None:
    page = "\n".join(f"## Part {i}\n" + ("words " * 300) for i in range(6))
    parts = chunks(page, max_chars=4000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == page


# --- structured extraction on the reference systems -------------------------------------------


def _structured(name: str) -> tuple[dict[str, float], SystemGraph]:
    d = ONBOARDING_DIR / name
    git = d / "sources/git"
    files = {p.relative_to(git).as_posix(): p.read_text() for p in git.rglob("*") if p.is_file()}
    cmdb = yaml.safe_load((d / "sources/servicenow/cmdb.yaml").read_text())
    live = yaml.safe_load((d / "sources/live/discovery.yaml").read_text())
    graph = build(structured_facts(files, cmdb, live))
    result = OnboardingResult(
        nodes=[ResultNode(id=n.id, kind=n.kind) for n in graph.nodes.values()],
        edges=[ResultEdge(src=a, kind=b, dst=c) for a, b, c in graph.edges],
    )
    return dict(score(load_reference_system(d), result)["graph"].metrics), graph


def test_system_b_is_described_by_its_code_and_discovery_alone() -> None:
    metrics, _ = _structured("system-b")
    assert metrics["node_precision"] == 1.0 and metrics["edge_precision"] == 1.0
    assert metrics["edge_recall"] >= 0.95  # only a docs-only edge is left to prose
    assert metrics["forbidden"] == 0


def test_system_a_needs_its_docs_for_the_schema_edges() -> None:
    metrics, graph = _structured("system-a")
    assert metrics["node_recall"] == 1.0 and metrics["forbidden"] == 0
    assert metrics["edge_recall"] < 0.8
    assert [q.about for q in graph.questions] == ["service:customer-service uses which database"]
    assert "service:refund-service" not in graph.nodes  # retired
    assert not any("ordersdb" in n for n in graph.nodes)  # 2023 MySQL


def test_superseded_and_draft_pages_are_never_read(tmp_path: Path) -> None:
    calls: list[str] = []

    class _Recorder:
        def extract(self, title: str, text: str, known: str) -> None:
            calls.append(title)

    built = build_system_graph(ONBOARDING_DIR / "system-b", _Recorder())  # type: ignore[arg-type]
    assert set(built.pages_skipped) == {"30002", "30003"}
    assert not any("superseded" in t.lower() or "draft" in t.lower() for t in calls)
    assert built.failed  # every answer was None, and that is reported


def test_a_generic_phrase_never_grounds_an_entity() -> None:
    """Seen live: "Every service is deployed ..." proposed as ``service:every-service``."""
    page = "Every service is deployed by its workflow to `prod-eu1`, namespace `payments`."
    extraction = GraphExtraction(
        entities=(ProseEntity("service:every-service", "Service", page),),
        facts=(ProseFact("service:every-service", "runs_on", "runtime:prod-eu1/payments", page),),
    )
    facts, rejected = ground(
        extraction, page_id="20005", page_text=page, known={"runtime:prod-eu1/payments"}
    )
    assert facts == []
    assert [r.why for r in rejected] == [
        "the quote doesn't name it",
        "unknown entity service:every-service",
    ]
