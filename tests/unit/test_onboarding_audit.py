"""ADR-0017 (WP-A6): every onboarding decision is in the event log, and shows on the page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from supportagent.adapters.db.repos.events import SqlEventSink  # noqa: F401
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.graph_review import GraphReview
from supportagent.app.onboarding_audit import OnboardingAudit, history
from supportagent.bootstrap import _event_sink
from supportagent.domain.events import EventKind
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.test_autopilot import _run

pytestmark = pytest.mark.unit


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def _history(db: Any, master_key: bytes, clock: Any) -> list[dict[str, object]]:
    uow = SqlUnitOfWork(db)
    with uow:
        return history(_event_sink(uow, master_key, clock), TENANT)


def test_a_run_records_each_step_it_ran(
    tmp_path: Path, db: Any, clock: Any, master_key: bytes
) -> None:
    _run(tmp_path, db, clock)
    actions = [(a["action"], a.get("step")) for a in _history(db, master_key, clock)]
    for step in ("ingest", "sense", "graph", "incidents", "requests", "readiness", "gaps"):
        assert ("step", step) in actions
    assert ("readiness_assessed", None) in actions and ("calibration_fitted", None) in actions
    _run(tmp_path, db, clock)  # done steps are skipped, so recorded once; blocked ones retried
    steps = [a["step"] for a in _history(db, master_key, clock) if a["action"] == "step"]
    assert steps.count("ingest") == 1 and steps.count("sense") == 1
    assert steps.count("incidents") == 2  # blocked on the model: retried, recorded again


def test_an_access_decision_and_a_graph_decision_are_audited_by_who_made_them(  # noqa: PLR0913, PLR0917 — fixtures
    tmp_path: Path,
    db: Any,
    clock: Any,
    ids: Any,
    master_key: bytes,
    client: TestClient,
    oidc: FakeOIDC,
) -> None:
    _run(tmp_path, db, clock)
    login(client, oidc, ADA)
    page = client.get("/ui/onboarding/requests").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    rid = re.search(r'action="/ui/onboarding/requests/([0-9a-f]+)"', page).group(1)  # type: ignore[union-attr]
    client.post(
        f"/ui/onboarding/requests/{rid}", data={"csrf_token": token, "decision": "approved"}
    )
    uow = SqlUnitOfWork(db)
    audit = OnboardingAudit(
        events=_event_sink(uow, master_key, clock), ids=ids, clock=clock, tenant_id=TENANT
    )
    review = GraphReview(uow=uow, clock=clock, tenant_id=TENANT, audit=audit)
    fact = review.facts(status="proposed")[0]
    review.decide([fact.id], "confirmed", actor="user_ada")
    events = _history(db, master_key, clock)
    access = next(a for a in events if a["action"] == "access_decided")
    assert access["decision"] == "approved" and access["actor"] == "sub-ada"  # who decided
    graph = next(a for a in events if a["action"] == "graph_decided")
    assert (graph["decision"], graph["facts"], graph["actor"]) == ("confirmed", 1, "user_ada")
    page = client.get("/ui/onboarding").text
    assert "Recent activity" in page and "access decided" in page and "style=" not in page


def test_audit_payloads_carry_no_floats() -> None:
    from supportagent.app.onboarding_audit import _no_floats  # noqa: PLC0415

    assert _no_floats({"share": 0.6312, "nested": {"x": 0.5}, "n": 3}) == {
        "share_bp": 6312,
        "nested": {"x_bp": 5000},
        "n": 3,
    }
    assert EventKind.ONBOARDING.value == "onboarding"


def test_onboarding_page_shows_the_ready_share_over_time(  # noqa: PLR0913, PLR0917 — fixtures
    tmp_path: Path,
    db: Any,
    clock: Any,
    ids: Any,
    master_key: bytes,
    client: TestClient,
    oidc: FakeOIDC,
) -> None:
    _run(tmp_path, db, clock)
    for share in (0.62, 0.75):
        clock.advance(3600)
        uow = SqlUnitOfWork(db)
        with uow:
            OnboardingAudit(
                events=_event_sink(uow, master_key, clock), ids=ids, clock=clock, tenant_id=TENANT
            ).record(
                "readiness_assessed",
                actor="onboarding",
                detail={"ready": share >= 0.7, "ready_share": share},
            )
            uow.commit()
    login(client, oidc, ADA)
    page = client.get("/ui/onboarding").text
    assert "Ready share over the last" in page
    assert "62% → 75%</p>" in page
