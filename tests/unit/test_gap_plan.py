"""WP-O5 (ADR-0015 §4): the gap plan, its requests, and reviewing them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.request_review import RequestReview, request_id
from supportagent.bootstrap import (
    ONBOARDING_DIR,
    build_gap_plan,
    build_incident_map,
    build_onboarding_pipeline,
    build_system_graph,
    detect_plugins,
    load_plugins,
    snapshot_from_sources,
)
from supportagent.config.loader import load_config_offline
from supportagent.domain.gap_plan import GapPlan, find_targets, target_name
from supportagent.domain.graph import Node, SystemGraph
from supportagent.domain.model.plugin import TargetFinder
from supportagent.domain.plugins import DocPage, SourceSnapshot
from tests.unit.api_support import ADA, TENANT, login, make_client

pytestmark = pytest.mark.unit


_LAB = Path(__file__).resolve().parents[2] / "evals/lab/lab.yaml"


def _plan(system: str) -> GapPlan:
    """The plan from the recorded readings (``evals/onboarding/recordings``)."""
    d = ONBOARDING_DIR / system
    plugins = load_plugins()
    detections = detect_plugins(d, plugins)
    pipeline = build_onboarding_pipeline(load_config_offline(_LAB), live=False)
    graph = build_system_graph(d, None).graph
    incidents = build_incident_map(d, detections, plugins, pipeline)
    return build_gap_plan(d, detections, plugins, graph, incidents)


def test_targets_are_named_without_the_kind_or_the_runtime_area() -> None:
    assert target_name("database:paydb") == "paydb"
    assert target_name("runtime:emr/emr-batch") == "emr-batch"
    assert target_name("runtime:prod-eu1/payments") == "prod-eu1/payments"


def test_each_finder_kind_finds_its_targets() -> None:
    graph = SystemGraph(
        nodes={
            "database:paydb": Node("database:paydb", "Database", {"oracle"}),
            "database:ordersdb": Node("database:ordersdb", "Database", {"mysql"}),
        }
    )
    snap = SourceSnapshot(
        repos=("github:acme/*", "gitlab:other/x"),
        cis=(("acme (Snowflake)", "cmdb_ci_cloud_database"), ("rs", "cmdb_ci_cloud_database")),
        docs=[
            DocPage("1", "Overview", frozenset(), "Behind Grafana (`grafana.acme.internal`)."),
            DocPage("2", "Old", frozenset({"legacy"}), "grafana.old.example"),
        ],
    )

    def names(**kw: Any) -> list[str]:
        return [t.name for t in find_targets(TargetFinder(**kw), graph, snap)]

    assert names(graph="Database", tech="oracle") == ["paydb"]
    assert names(repos="github") == ["acme/*"]
    assert names(cmdb_class="cmdb_ci_cloud_database", name=r"^(.+) \(Snowflake\)$") == ["acme"]
    assert names(docs=r"\b(grafana(?:\.[a-z0-9-]+)+\.[a-z]{2,})\b") == ["grafana.acme.internal"]


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({}, "exactly one of"),
        ({"graph": "Database", "repos": "github"}, "exactly one of"),
        ({"docs": "grafana"}, "exactly one group"),
        ({"cmdb_class": "x", "name": "("}, "bad regex"),
    ],
)
def test_a_bad_finder_does_not_load(kwargs: dict[str, str], why: str) -> None:
    with pytest.raises(ValidationError, match=why):
        TargetFinder(**kwargs)


def test_system_a_asks_for_read_only_access_ranked_by_what_it_unlocks() -> None:
    plan = _plan("system-a")
    keys = [r.key for r in plan.requests]
    assert set(keys) == {
        ("oracle", "oracle_ro", "paydb"),
        ("oracle", "oracle_ro", "crmdb"),
        ("kubernetes", "k8s_ro", "prod-eu1/payments"),
        ("grafana", "grafana_ro", "grafana.acme.internal"),
        ("github", "github_ro", "acme-payments/*"),
    }
    assert [r.share for r in plan.requests] == sorted(
        (r.share for r in plan.requests), reverse=True
    )
    paydb = next(r for r in plan.requests if r.target == "paydb")
    crmdb = next(r for r in plan.requests if r.target == "crmdb")
    # Lock contention is on payment-service, which uses PAYDB: only paydb unlocks it.
    assert "oracle-lock-contention" in paydb.unlocks
    assert "oracle-lock-contention" not in crmdb.unlocks
    assert paydb.scope.startswith("CREATE SESSION; SELECT on")  # the contract, verbatim
    assert "recent-change-push" in next(r for r in plan.requests if r.plugin == "github").unlocks


def test_system_b_skips_retired_cis_and_explains_each_gap() -> None:
    plan = _plan("system-b")
    targets = {r.target for r in plan.requests}
    assert "acme-analytics" in targets and "analytics-rs" not in targets  # Redshift is retired
    assert {r.target for r in plan.requests if r.plugin == "spark"} == {
        "emr-batch",
        "emr-streaming",
    }
    assert all(g.why for g in plan.gaps)


def test_the_review_keeps_decisions_across_plans(db: Any, clock: Any) -> None:
    review = RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    plan = _plan("system-a")
    assert review.save(plan) == {"new": 5, "kept": 0, "gone": 0}
    rid = request_id("oracle", "oracle_ro", "paydb")
    assert review.decide(rid, "approved", actor="user_ada").status == "approved"
    smaller = GapPlan(tuple(r for r in plan.requests if r.target != "crmdb"), (), plan.unlocked)
    assert review.save(smaller) == {"new": 0, "kept": 4, "gone": 1}
    assert next(r for r in review.requests() if r.id == rid).decided_by == "user_ada"
    assert review.progress() == {"open": 3, "approved": 1, "declined": 0}
    with pytest.raises(Exception, match="unknown decision"):
        review.decide(rid, "maybe", actor="u")  # type: ignore[arg-type]


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def test_the_requests_page_and_a_decision(
    client: TestClient, oidc: FakeOIDC, db: Any, clock: Any
) -> None:
    RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).save(_plan("system-a"))
    login(client, oidc, ADA)
    assert "access requests to decide" in client.get("/ui/onboarding").text
    page = client.get("/ui/onboarding/requests").text
    assert "grafana.acme.internal" in page and "<meter" in page and "style=" not in page
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    rid = request_id("github", "github_ro", "acme-payments/*")
    done = client.post(
        f"/ui/onboarding/requests/{rid}",
        data={"csrf_token": token, "decision": "declined"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert "Declined by" in client.get("/ui/onboarding/requests").text


def test_the_lab_snapshot_lists_repos_and_installed_cis() -> None:
    snap = snapshot_from_sources(ONBOARDING_DIR / "system-b")
    assert snap.repos == ("github:acme-analytics/*",)
    assert ("acme-analytics (Snowflake)", "cmdb_ci_cloud_database") in snap.cis
    assert not any(name.startswith("analytics-rs") for name, _ in snap.cis)
