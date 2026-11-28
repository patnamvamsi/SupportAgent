"""The Evidence Room UI: the system explorer's data, packaged static files, and the case story.

The explorer (``/ui/system``) draws ``/ui/system/graph.json`` in 3D; the case page lays
out triage's questions in ARCH §4 order and the trace as a replay (``case_story``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app import system_view
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event
from supportagent.entrypoints.api import case_story
from tests.unit.api_support import ADA, BOB, TENANT, login, make_client, seed_users
from tests.unit.test_system_view import _save

pytestmark = pytest.mark.unit


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes) -> tuple[TestClient, FakeOIDC]:
    oidc = FakeOIDC()
    return make_client(db, clock, ids, master_key, oidc), oidc


def test_graph_json_draws_every_live_fact_with_its_cases(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    facts = _save(db, clock).facts()
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, status,"
                " opened_ts, last_alert_ts, trace_id) VALUES ('case_1', :t, 'fp1', 'alert',"
                " 'feed', 'triaged', 100, 200, 'tr_1')"
            ),
            {"t": TENANT},
        )
    http, oidc = client
    assert http.get("/ui/system/graph.json").status_code == 401, "signed out"
    login(http, oidc, ADA)
    graph = http.get("/ui/system/graph.json").json()
    nodes = {n["id"]: n for n in graph["nodes"]}
    feed = nodes["service:feed"]
    assert feed["component"] is True and feed["open"] == 1 and feed["cases"] == 1
    assert feed["groups"]["data flow"] >= 1
    assert feed["sources"] and all(s.startswith("git:") for s in feed["sources"]), "cited"
    assert nodes["external:newsvendor"]["component"] is False
    assert {(e["src"], e["dst"]) for e in graph["edges"]} >= {
        ("service:feed", "external:newsvendor")
    }
    ends = {e for edge in graph["edges"] for e in (edge["src"], edge["dst"])}
    assert ends <= set(nodes), "every edge's ends are drawn"
    live = [f for f in facts if f.fact == "edge" and f.status != "rejected" and f.present]
    assert len(graph["edges"]) == len(live)


def test_graph_json_needs_the_view_permission(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    seed_users(db, ("sub-ada", "admin"), ("sub-bob", None))
    http, oidc = client
    login(http, oidc, BOB)
    assert http.get("/ui/system/graph.json").status_code == 403


def test_an_edge_to_an_undeclared_node_still_draws() -> None:
    from supportagent.app.graph_review import FactView  # noqa: PLC0415

    edge = FactView(
        id="f1", fact="edge", src="service:a", kind="publishes", dst="topic:t", tech=(),
        sources=(), layer=1, status="proposed", present=True, reviewed_by=None,
    )  # fmt: skip
    node = FactView(
        id="f2", fact="node", src="service:a", kind="Service", dst=None, tech=(),
        sources=(), layer=1, status="confirmed", present=True, reviewed_by=None,
    )  # fmt: skip
    graph = system_view.graph_data([node, edge])
    topic = next(n for n in graph["nodes"] if n["id"] == "topic:t")
    assert topic["kind"] == "topic" and topic["status"] == "proposed"


def test_static_serves_packaged_files_only(client: tuple[TestClient, FakeOIDC]) -> None:
    http, _ = client
    css = http.get("/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    three = http.get("/static/vendor/three/three.min.js")
    assert three.status_code == 200 and "javascript" in three.headers["content-type"]
    assert http.get("/static/vendor/three/LICENSE").status_code == 200, "its licence ships"
    font = http.get("/static/fonts/public-sans-latin.woff2")
    assert font.headers["content-type"] == "font/woff2"
    for path in ("/static/../routes/ui.py", "/static/nope.js", "/static/vendor/"):
        assert http.get(path).status_code == 404, path


def test_pages_load_only_same_origin_scripts(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    _save(db, clock)
    http, oidc = client
    login(http, oidc, ADA)
    page = http.get("/ui/system")
    assert "'unsafe-inline'" not in page.headers["content-security-policy"]
    assert '<script src="/static/graph3d.js"' in page.text
    assert "<script>" not in page.text and " style=" not in page.text, "CSP: nothing inline"
    assert 'aria-current="page"' in page.text and "Every component" in page.text


_DECISION: dict[str, object] = {
    "outcome": "safe_default",
    "reason": "low-calibrated-score",
    "classification": "runtime/down",
    "severity": "P1",
    "preclass_label": "runtime/down",
    "calibrated_score": 0.42,
}


def test_gates_stop_where_triage_stopped() -> None:
    gates = case_story.gates(_DECISION, service="writer")
    assert [g.state for g in gates] == ["passed"] * 3 + ["stopped"] + ["not_reached"] * 3
    assert gates[0].detail == "It belongs to writer."
    assert gates[2].detail == "The rules also say runtime/down."
    assert "0.42" in gates[3].detail and "under the floor" in gates[3].detail


def test_gates_for_a_routed_case_and_a_stop_before_triage() -> None:
    routed = case_story.gates(
        {"outcome": "routed", "reason": "routed", "route_id": "r1", "severity": "P3"}
    )
    assert [g.state for g in routed][-1] == "routed" and routed[-1].detail == "Routed on r1."
    assert routed[4].detail == "P3 needs no cross-check."
    storm = case_story.gates({"outcome": "safe_default", "reason": "storm-member"})
    assert storm[0].state == "stopped" and "alert storm" in storm[0].detail
    assert all(g.state == "not_reached" for g in storm[1:])
    assert case_story.gates(None) == []


def test_timeline_offsets_and_depth() -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)

    def event(n: int, kind: EventKind, parent: str | None, ms: int) -> Event:
        return Event(
            id=f"ev_{n}", tenant_id=TENANT, trace_id="tr_1", span_id=f"sp_{n}",
            parent_span_id=parent, kind=kind, actor="agent", payload={"v": 1},
            ts=start + timedelta(milliseconds=ms), latency_ms=5,
        )  # fmt: skip

    steps = case_story.timeline(
        [
            event(1, EventKind.INGEST, None, 0),
            event(2, EventKind.LLM, "sp_1", 40),
            event(3, EventKind.TOOL_CALL, "sp_2", 90),
        ]
    )
    assert [(s.kind, s.at_ms, s.depth) for s in steps] == [
        ("ingest", 0, 0),
        ("llm", 40, 1),
        ("tool_call", 90, 2),
    ]
    assert case_story.timeline([]) == []


def test_open_cases_reach_the_rail_the_graph_and_the_system_panel(
    db: Any, clock: Any, client: tuple[TestClient, FakeOIDC]
) -> None:
    import json  # noqa: PLC0415

    _save(db, clock)
    rca = {
        "outcome": "SUPPORTED",
        "reason": "container down",
        "plan": "runbook:runtime-down",
        "evidence": [
            {"step": "a", "hypothesis": "h", "ok": True, "ran": True},
            {"step": "b", "hypothesis": "h", "ok": False, "ran": True},
        ],
        "claims": [],
    }
    with db.begin() as conn:
        rows = (
            ("case_1", "P3", "triaged"),
            ("case_2", "P1", "triaged"),
            ("case_3", "P1", "resolved"),
        )
        for case_id, severity, status in rows:
            conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, fingerprint, origin, source, service,"
                    " classification, severity, status, decision_json, opened_ts, last_alert_ts,"
                    " trace_id) VALUES (:id, :t, :fp, 'alert', 'webhook:ContainerDown', 'feed',"
                    " 'runtime/down', :sev, :st, :dj, 100, 200, :tr)"
                ),
                {
                    "id": case_id, "t": TENANT, "fp": f"fp_{case_id}", "sev": severity,
                    "st": status, "tr": f"tr_{case_id}", "dj": json.dumps(_DECISION),
                },
            )  # fmt: skip
        conn.execute(
            text(
                "INSERT INTO rca_artifacts (id, tenant_id, case_id, outcome, artifact_json,"
                " created_ts) VALUES ('rca_2', :t, 'case_2', 'SUPPORTED', :aj, 0)"
            ),
            {"t": TENANT, "aj": json.dumps(rca)},
        )
    http, oidc = client
    login(http, oidc, ADA)
    page = http.get("/ui/system").text
    assert 'title="open cases">2<' in page, "the rail counts open cases"
    assert 'href="/ui/cases/case_2"' in page, "the panel opens on the P1, not the P3"
    assert "Is the label proven on this system?" in page and "E-01" in page
    paths = http.get("/ui/system/graph.json").json()["cases"]
    assert [(p["id"], p["node"], p["held"], p["exhibits"]) for p in paths] == [
        ("case_2", "service:feed", 1, 2),
        ("case_1", "service:feed", 0, 0),
    ], "open cases only, most severe first"
