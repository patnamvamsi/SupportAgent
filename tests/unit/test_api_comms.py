"""WP-102: ``/api/v1/comms-plans/{id}[/approve|/revoke]`` (02 §2)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.files.comms_loader import load_comms_catalog
from supportagent.app.comms import CommsPlanner
from supportagent.app.gate import GateService
from supportagent.domain.trace import TraceContext
from tests.unit.api_support import (
    ADA,
    BOB,
    CARL,
    TENANT,
    catalog,
    csrf,
    login,
    logout,
    make_client,
    seed_proposal,
    seed_users,
)

_REPO = Path(__file__).resolve().parents[2]


def _plan(db: Any, clock: Any, ids: Any, events: Any) -> str:
    seed_proposal(db, proposal_id="p_wn", case_id="case_1", status="executed")
    uow = SqlUnitOfWork(db)
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments"),
        ttl=timedelta(hours=24),
    )
    plans = load_comms_catalog(_REPO / "templates" / "comms").plans
    CommsPlanner(uow=uow, ids=ids, gate=gate, plans=plans, tenant_id=TENANT).plan(
        "case_1", "customer-4h", TraceContext.new(TENANT, "tr_p_wn", "s")
    )
    with db.connect() as conn:
        return str(conn.execute(text("SELECT id FROM comms_plans")).scalar_one())


def test_comms_plan_api_approve_and_revoke(
    db: Any, clock: Any, ids: Any, master_key: bytes, events: Any
) -> None:
    oidc = FakeOIDC()
    client: TestClient = make_client(db, clock, ids, master_key, oidc)
    plan_id = _plan(db, clock, ids, events)
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "approver"), ("sub-carl", "viewer"))
    login(client, oidc, CARL)
    plan = client.get(f"/api/v1/comms-plans/{plan_id}").json()
    assert plan["status"] == "pending_approval"
    assert plan["allowed_fields"] == {
        "next_update_eta": "schedule",
        "priority": "record_state",
        "state": "record_state",
    }
    body = {"payload_hash": plan["plan_hash"]}
    viewer = client.post(f"/api/v1/comms-plans/{plan_id}/approve", json=body, headers=csrf(client))
    assert viewer.status_code == 403
    logout(client)
    login(client, oidc, BOB)
    stale = client.post(
        f"/api/v1/comms-plans/{plan_id}/approve",
        json={"payload_hash": "0" * 64},
        headers=csrf(client),
    )
    assert stale.status_code == 409, "the approver approves exactly the plan they saw"
    ok = client.post(f"/api/v1/comms-plans/{plan_id}/approve", json=body, headers=csrf(client))
    assert ok.status_code == 200, ok.text
    assert client.get(f"/api/v1/comms-plans/{plan_id}").json()["status"] == "approved"
    case = client.get("/api/v1/cases/case_1").json()
    assert [p["status"] for p in case["comms_plans"]] == ["approved"]
    revoked = client.post(f"/api/v1/comms-plans/{plan_id}/revoke", headers=csrf(client))
    assert revoked.status_code == 200
    assert client.get(f"/api/v1/comms-plans/{plan_id}").json()["status"] == "revoked"
    again = client.post(f"/api/v1/comms-plans/{plan_id}/revoke", headers=csrf(client))
    assert again.status_code == 422
    assert "Comms plans" in client.get("/ui/cases/case_1").text
