"""WP-O3b (ADR-0015 decision 4): reviewing the system graph, in the service and in the UI."""

from __future__ import annotations

import html
import re
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.app.graph import build_graph
from supportagent.app.graph_review import FactView, GraphReview, fact_id
from supportagent.bootstrap import ONBOARDING_DIR, discovery_rules, load_plugins
from supportagent.domain.graph import EdgeFact
from supportagent.entrypoints.api.graph_svg import render
from tests.unit.api_support import ADA, BOB, TENANT, login, logout, make_client, seed_users

pytestmark = pytest.mark.unit

_DISCOVERY = discovery_rules(load_plugins())

_QUOTE = (
    "| orders-service | Creates and tracks orders "
    "| Oracle `PAYDB` (schema ORDERS), payment-service |"
)
_URL = "https://wiki.acme.test/pages/10001"


def _system_a(extra: list[EdgeFact] | None = None) -> Any:
    d = ONBOARDING_DIR / "system-a"
    git = d / "sources/git"
    files = {p.relative_to(git).as_posix(): p.read_text() for p in git.rglob("*") if p.is_file()}
    cmdb = yaml.safe_load((d / "sources/servicenow/cmdb.yaml").read_text())
    live = yaml.safe_load((d / "sources/live/discovery.yaml").read_text())
    facts = [*structured_facts(files, cmdb, live, (), _DISCOVERY), *(extra or [])]
    return build_graph(facts, [], None)


_PROSE = EdgeFact(
    "service:orders-service",
    "uses",
    "schema:paydb.orders",
    "confluence:10001",
    3,
    "extract_graph",
    _QUOTE,
)


def _review(db: Any, clock: Any) -> GraphReview:
    return GraphReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)


def test_saving_keeps_decisions_and_marks_what_is_gone(db: Any, clock: Any) -> None:
    review = _review(db, clock)
    first = review.save(_system_a([_PROSE]), {"confluence:10001": _URL})
    assert first["new"] > 30 and first["gone"] == 0
    edge = fact_id("edge", "service:orders-service", "uses", "schema:paydb.orders")
    assert review.decide([edge], "confirmed", actor="user_ada") == 1
    [view] = [f for f in review.facts() if f.id == edge]
    assert view.layer == 3 and view.layer_label == "documentation"
    assert view.sources[0] == {"ref": "confluence:10001", "url": _URL, "quote": _QUOTE}

    # The docs fact isn't found this time; the coarse database edge it had
    # subsumed (orders-service uses database:paydb) comes back as new.
    again = review.save(_system_a(), {})
    assert again["new"] == 1 and again["gone"] == 1
    assert edge not in {f.id for f in review.facts()}
    [gone] = [f for f in review.facts(include_gone=True) if f.id == edge]
    assert gone.status == "confirmed" and not gone.present  # the decision isn't lost

    review.save(_system_a([_PROSE]), {})
    [back] = [f for f in review.facts() if f.id == edge]
    assert back.status == "confirmed" and back.present


def test_a_trusted_layer_is_confirmed_at_once_and_progress_counts_it(db: Any, clock: Any) -> None:
    review = _review(db, clock)
    review.save(_system_a([_PROSE]), {})
    before = review.progress()
    assert before["reviewed"] == 0 and before["open_questions"] == 1
    confirmed = review.decide_layer(1, "confirmed", actor="user_ada")
    after = review.progress()
    assert after["confirmed"] == confirmed > 0
    by_layer = after["by_layer"]
    assert isinstance(by_layer, dict)
    assert by_layer[3]["proposed"] == 1  # the docs fact still waits for a person
    assert by_layer[1]["label"] == "code, config and the CMDB"
    with pytest.raises(Exception, match="unknown decision"):
        review.decide(["x"], "maybe", actor="u")  # type: ignore[arg-type]


def test_settling_a_question_can_add_the_edge_a_person_vouches_for(db: Any, clock: Any) -> None:
    review = _review(db, clock)
    review.save(_system_a(), {})
    [question] = review.questions()
    assert question.about == "service:customer-service uses which database"
    assert [o["label"] for o in question.options][1] == (
        "The CMDB is right: it uses database:paydb"
    )
    review.resolve(question.id, 1, actor="user_ada")
    [settled] = review.questions()
    assert settled.status == "resolved" and settled.resolved_by == "user_ada"
    added = fact_id("edge", "service:customer-service", "uses", "database:paydb")
    [edge] = [f for f in review.facts() if f.id == added]
    assert (edge.status, edge.layer, edge.sources[0]["ref"]) == ("confirmed", 0, "review:user_ada")


def test_the_drawing_shows_status_and_escapes_names() -> None:
    def fact(src: str, kind: str, dst: str | None, status: str) -> FactView:
        return FactView(
            fact_id("edge" if dst else "node", src, kind, dst),
            "edge" if dst else "node",
            src,
            kind,
            dst,
            (),
            (),
            1,
            status,
            True,
            None,
        )

    svg = str(
        render(
            [
                fact("service:a", "Service", None, "confirmed"),
                fact("service:<script>alert(1)</script>", "Service", None, "proposed"),
                fact("service:a", "calls", "service:b", "proposed"),
                fact("service:a", "calls", "service:c", "rejected"),
            ],
            focus="service:a",
        )
    )
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert 'class="edge proposed focus"' in svg
    assert "service:c" not in svg  # a rejected edge isn't drawn
    assert "/ui/onboarding/graph?focus=service%3Aa" in svg


# --- the pages -------------------------------------------------------------------------------


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match, f"form field {name!r} missing"
    return html.unescape(match.group(1))


def test_the_review_pages_and_their_decisions(
    client: TestClient, oidc: FakeOIDC, db: Any, clock: Any
) -> None:
    _review(db, clock).save(_system_a([_PROSE]), {"confluence:10001": _URL})
    login(client, oidc, ADA)
    overview = client.get("/ui/onboarding").text
    assert "reviewed <strong>0 of" in overview and "1 question to settle" in overview
    assert 'href="/ui/onboarding"' in overview  # the nav link

    page = client.get("/ui/onboarding/graph?focus=service:orders-service").text
    assert (
        "<svg" in page
        and _URL in page
        and html.escape(_QUOTE, quote=False) in page.replace("&#39;", "'")
    )
    token = _field(page, "csrf_token")
    edge = fact_id("edge", "service:orders-service", "uses", "schema:paydb.orders")
    one = client.post(
        "/ui/onboarding/facts",
        data={"csrf_token": token, "one": f"{edge}|confirmed", "back": "/ui/onboarding/graph"},
        follow_redirects=False,
    )
    assert one.status_code == 303 and one.headers["location"] == "/ui/onboarding/graph"
    review = _review(db, clock)
    assert next(f for f in review.facts() if f.id == edge).status == "confirmed"

    others = [f.id for f in review.facts(status="proposed", focus="service:orders-service")][:2]
    bulk = client.post(
        "/ui/onboarding/facts",
        data={"csrf_token": token, "decision": "rejected", **{f"id_{i}": "on" for i in others}},
        follow_redirects=False,
    )
    assert bulk.status_code == 303
    assert {f.id for f in review.facts(status="rejected")} == set(others)

    layer = client.post(
        "/ui/onboarding/layer",
        data={"csrf_token": token, "layer": "2", "decision": "confirmed"},
        follow_redirects=False,
    )
    assert layer.status_code == 303 and not review.facts(status="proposed", layer=2)

    questions = client.get("/ui/onboarding/questions").text
    qid = re.search(r'action="/ui/onboarding/questions/([0-9a-f]+)"', questions).group(1)  # type: ignore[union-attr]
    settled = client.post(
        f"/ui/onboarding/questions/{qid}",
        data={"csrf_token": token, "choice": "0"},
        follow_redirects=False,
    )
    assert settled.status_code == 303
    assert "Settled by" in client.get("/ui/onboarding/questions").text
    assert client.post("/ui/onboarding/facts", data={"one": f"{edge}|rejected"}).status_code == 403


def test_every_fact_to_review_under_a_filter_is_decided_at_once(
    client: TestClient, oidc: FakeOIDC, db: Any, clock: Any
) -> None:
    review = _review(db, clock)
    review.save(_system_a([_PROSE]), {"confluence:10001": _URL})
    login(client, oidc, ADA)
    focus = "service:orders-service"
    page = client.get(f"/ui/onboarding/graph?focus={focus}").text
    shown = review.facts(status="proposed", focus=focus)
    assert f"Confirm all {len(shown)}" in page and 'name="every"' in page
    others = {f.id for f in review.facts(status="proposed")} - {f.id for f in shown}
    assert others, "facts outside the filter"
    done = client.post(
        "/ui/onboarding/facts",
        data={"csrf_token": _field(page, "csrf_token"), "every": "confirmed", "focus": focus},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert not review.facts(status="proposed", focus=focus)
    assert {f.id for f in review.facts(status="proposed")} == others, "only what was shown"
    page = client.get("/ui/onboarding/graph").text
    client.post(
        "/ui/onboarding/facts",
        data={"csrf_token": _field(page, "csrf_token"), "every": "confirmed"},
    )
    assert not review.facts(status="proposed")
    assert "Confirm all" not in client.get("/ui/onboarding/graph").text, "nothing left"


def test_a_viewer_cannot_review(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "viewer"))
    login(client, oidc, BOB)
    page = client.get("/ui/onboarding")
    assert page.status_code == 403 and "review_knowledge" in page.text
    assert 'href="/ui/onboarding"' not in client.get("/ui/approvals").text


def test_the_review_script_is_served_under_the_csp(client: TestClient) -> None:
    script = client.get("/static/review.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith(
        "text/javascript"
    )
    assert "script-src 'self'" in script.headers["Content-Security-Policy"]
