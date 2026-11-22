"""WP-225: the system view (ADR-0023 §3).

Canonical test IDs from 12-wp WP-225: ``test_component_page_shows_cited_facts``,
``test_case_links_to_its_component``, ``test_graph_layers_filter``. The graph is
the code layer's synthetic system (``test_code_layer``), saved for review as
onboarding saves it, and read back through the UI.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.app import system_view
from supportagent.app.graph import GraphBuild
from supportagent.app.graph_review import GraphReview
from supportagent.domain.graph import build
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.test_code_layer import _declared, _system

pytestmark = pytest.mark.unit


def _save(db: Any, clock: Any) -> GraphReview:
    facts = structured_facts(_system(), {}, {}, _declared())
    review = GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    review.save(GraphBuild(graph=build(facts), facts=facts), {})
    return GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes) -> tuple[TestClient, FakeOIDC]:
    oidc = FakeOIDC()
    return make_client(db, clock, ids, master_key, oidc), oidc


def test_graph_layers_filter(db: Any, clock: Any) -> None:
    facts = _save(db, clock).facts()
    found = system_view.components(facts)
    feed = found["service:feed"]
    assert {r.verb for r in feed.groups["data flow"]} == {"publishes"}
    assert {r.target for r in feed.groups["storage"]} >= {"table:runner_cursors"}
    assert [r.target for r in feed.groups["work"]] == ["job:feed.sync_bars"]
    assert {r.target for r in feed.groups["interfaces"]} == {"external:newsvendor"}
    ticks = next(r for r in found["service:trader"].groups["data flow"] if "ticks" in r.target)
    assert ("service:feed", "publishes") in ticks.others, "who's on the other side"
    overview = system_view.overview(facts)
    assert overview.components[0].kind == "Service" and overview.kinds["Topic"] == 2


def test_component_page_shows_cited_facts(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    _save(db, clock)
    http, oidc = client
    login(http, oidc, ADA)
    system = http.get("/ui/system").text
    assert 'href="/ui/system/component?id=service%3Afeed"' in system
    page = http.get("/ui/system/component?id=service:feed").text
    assert "publishes" in page and "ticks.*" in page
    assert "git:platform/feed/app.py:" in page, "each fact cites its line"
    assert "trader" in page, "the consumer on the other side of the topic"
    assert http.get("/ui/system/component?id=service:nope").status_code == 404


def test_case_links_to_its_component(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    _save(db, clock)
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, source, service,"
                " classification, severity, status, decision_json, opened_ts, last_alert_ts,"
                " trace_id) VALUES ('case_1', :t, 'fp1', 'alert', 'webhook:alertmanager',"
                " 'feed', 'runtime/stalled', 'P2', 'triaged',"
                ' \'{"outcome": "safe_default", "reason": "low-calibrated-score"}\','
                " 100, 200, 'tr_1')"
            ),
            {"t": TENANT},
        )
    http, oidc = client
    login(http, oidc, ADA)
    cases = http.get("/ui/cases").text
    assert 'href="/ui/cases/case_1"' in cases and "safe default" in cases
    assert "feed" in http.get("/ui/cases?service=feed").text
    assert "No cases match" in http.get("/ui/cases?service=trader").text
    component = http.get("/ui/system/component?id=service:feed").text
    assert 'href="/ui/cases/case_1"' in component, "the component lists its cases"
    assert "1 open case" in http.get("/ui/system").text


def test_defined_not_running_marked(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    """WP-221 part 2: a file can define a service nobody runs any more."""
    facts = _save(db, clock).facts()
    found = system_view.components(facts, observed=["feed", "trader_worker", "broker"])
    assert found["service:feed"].running is True
    assert found["service:trader-worker"].running is True, "_ and - name one service"
    assert found["service:trader"].running is False, "defined, not running"
    assert found["job:feed.sync_bars"].running is None, "only services run as containers"
    assert all(c.running is None for c in system_view.components(facts).values()), "unmeasured"
    ranked = [c.id for c in system_view.overview(facts, ["feed", "broker"]).components]
    assert ranked.index("service:trader") > ranked.index("service:broker"), "running first"

    from supportagent.app.detector_review import DetectorReview, DetectorReviewStore  # noqa: PLC0415

    DetectorReview(
        DetectorReviewStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT), clock
    ).save_measurement([], [], system="platform", days=7, observed=["feed"])
    http, oidc = client
    login(http, oidc, ADA)
    page = http.get("/ui/system").text
    assert "defined, not running" in page and ">running<" in page
