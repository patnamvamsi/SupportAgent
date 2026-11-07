"""ADR-0017 (WP-A1): one resumable ``onboard run``, and what's needed from a person.

The system is ingested through fake ServiceNow, Confluence and GitHub
servers, as a real one would be. The model never answers here
(``_UnrecordedProvider``), so this proves the loop's mechanics, not the
model: the onboarding lab measures that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from typer.testing import CliRunner

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.fakes.servicenow import SyncASGITransport
from supportagent.app.request_review import RequestReview
from supportagent.bootstrap import (
    AUTOPILOT_STATE,
    ONBOARDING_DIR,
    IngestTransports,
    _UnrecordedProvider,
    onboard_run,
)
from supportagent.domain.autopilot import Need, OnboardingStatus, StepRecord, digest, should_run
from supportagent.entrypoints.cli.main import app
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.test_ingest import (
    _ENV,
    _fake_confluence,
    _fake_github,
    _fake_servicenow,
    _manifest,
)
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

#: The test master key (tests/conftest.py's ``master_key`` fixture uses the same bytes).
_MASTER_KEY = b"test" * 8

_SYSTEM = ONBOARDING_DIR / "system-c"


def _transports() -> IngestTransports:
    return IngestTransports(
        servicenow=SyncASGITransport(_fake_servicenow(_SYSTEM).app),
        confluence=_fake_confluence(_SYSTEM),
        github=_fake_github(_SYSTEM, "acme-logistics"),
    )


def _run(tmp_path: Path, db: Any, clock: Any) -> OnboardingStatus:
    config = _shadow_config()
    config = config.model_copy(update={"tenant": config.tenant.model_copy(update={"id": TENANT})})
    return onboard_run(
        config,
        _manifest(_SYSTEM, tmp_path),
        tmp_path / "ws",
        provider=_UnrecordedProvider(),
        engine=db,
        clock=clock,
        transports=_transports(),
        env=_ENV,
        master_key=_MASTER_KEY,
    )


def test_one_command_takes_a_new_system_through_every_step(
    tmp_path: Path, db: Any, clock: Any
) -> None:
    status = _run(tmp_path, db, clock)
    steps = {s.step: s for s in status.steps}
    assert list(steps) == [
        "ingest",
        "sense",
        "graph",
        "incidents",
        "requests",
        "evidence",
        "readiness",
        "gaps",
    ]
    # ADR-0018: no configured tool reads the past, so the workspace's evidence stands.
    assert "no tool here reads the past" in steps["evidence"].detail
    # The model never answers here: the steps that need it say so, and nothing claims ready.
    blocked = {s.step for s in status.steps if s.state == "blocked"}
    assert blocked == {"graph", "incidents"}, status.lines()
    assert all(steps[s].on == "model" for s in blocked)
    assert {s.state for s in status.steps if s.step not in blocked} == {"done"}
    assert status.ready_share == 0.0 and not status.ready
    assert any(n.kind == "source" and "incidents" in n.what for n in status.needs)
    assert "nodejs" in steps["sense"].detail and "mongodb" in steps["sense"].detail
    access = {n.what for n in status.needs if n.kind == "access"}
    assert "rabbitmq/rabbitmq_ro on logistics-mq" in access
    assert "mongodb/mongodb_ro on shipmentsdb" in access
    assert any(n.kind == "review" for n in status.needs)  # the graph facts to confirm
    assert status.bar == 0.70
    saved = json.loads((tmp_path / "ws" / AUTOPILOT_STATE).read_text())
    assert saved["system"] == "shipment-tracking" and len(saved["steps"]) == 8


def test_a_rerun_skips_what_is_done_and_clears_what_a_person_did(
    tmp_path: Path, db: Any, clock: Any
) -> None:
    first = _run(tmp_path, db, clock)
    review = RequestReview(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    approved, *declined = review.requests()
    review.decide(approved.id, "approved", actor="user_ada")
    for request in declined:
        review.decide(request.id, "declined", actor="user_ada")
    clock.advance(300)
    again = _run(tmp_path, db, clock)
    before = {s.step: s for s in first.steps}
    for step in again.steps:  # done steps are skipped; blocked ones are retried
        assert (step.at == before[step.step].at) == (before[step.step].state == "done")
    # ADR-0019: a declined request is done; an approved one waits for its credential.
    access = [n.what for n in again.needs if n.kind == "access"]
    assert access == [
        f"{approved.plugin}/{approved.access} on {approved.target}: approved, "
        f"bind it with `supportagent onboard bind {approved.id}`"
    ]
    assert len(again.needs) < len(first.needs)


def test_a_live_case_resolving_runs_readiness_again(tmp_path: Path, db: Any, clock: Any) -> None:
    """ADR-0020: it calibrates a label or proves a class, so a rerun must read it."""
    first = {s.step: s for s in _run(tmp_path, db, clock).steps}
    clock.advance(300)
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, decision_json, "
                "opened_ts, last_alert_ts, trace_id, ticket_number) VALUES ('case_live', :t, "
                "'fp', 'ticket', 'resolved', '{}', 100, 100, 'tr', 'INC-LIVE')"
            ),
            {"t": TENANT},
        )
    again = {s.step: s for s in _run(tmp_path, db, clock).steps}
    assert again["readiness"].at != first["readiness"].at
    assert again["incidents"].at == first["incidents"].at or first["incidents"].state != "done"


def test_status_reads_the_last_run(tmp_path: Path, db: Any, clock: Any) -> None:
    _run(tmp_path, db, clock)
    result = CliRunner().invoke(app, ["onboard", "status", "--workspace", str(tmp_path / "ws")])
    assert result.exit_code == 0, result.output
    assert "shipment-tracking" in result.output and "needed from you" in result.output
    empty = CliRunner().invoke(app, ["onboard", "status", "--workspace", str(tmp_path / "none")])
    assert empty.exit_code == 1 and "not onboarded yet" in empty.output


def test_a_step_runs_unless_done_on_the_same_inputs() -> None:
    done = StepRecord("graph", "done", digest("a"), "d", "t")
    assert not should_run(done, digest("a"))
    assert should_run(done, digest("b"))
    assert should_run(StepRecord("graph", "blocked", digest("a"), "d", "t", on="user"), digest("a"))
    assert should_run(None, digest("a"))
    assert digest([("x", "1"), ("y", "2")]) == digest([("y", "2"), ("x", "1")])  # order-free


def test_needs_are_ranked_by_the_weight_they_hold_back() -> None:
    status = OnboardingStatus(
        system="s",
        needs=[
            Need("review", "3 facts", 0.0, "/ui/onboarding"),
            Need("access", "mongodb on ordersdb", 0.4, "/ui/onboarding/requests"),
            Need("plugin", "angular: skeleton", 0.1, "add it"),
        ],
        ready_share=0.5,
        bar=0.7,
    )
    assert [n.kind for n in status.ranked()] == ["access", "plugin", "review"]
    assert not status.ready
    assert any("Grant access" in line for line in status.lines())


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def test_the_overview_shows_the_run_and_what_is_needed(
    tmp_path: Path, db: Any, clock: Any, client: TestClient, oidc: FakeOIDC
) -> None:
    login(client, oidc, ADA)
    assert "Needed from you" not in client.get("/ui/onboarding").text  # not onboarded yet
    _run(tmp_path, db, clock)
    page = client.get("/ui/onboarding").text
    assert "Not ready to support yet" in page and "Needed from you" in page
    assert "Grant access:" in page and 'href="/ui/onboarding/requests"' in page
    assert "check the model, then re-run" in page  # the blocked steps, as a need
    assert '<li class="step blocked">' in page and "style=" not in page
