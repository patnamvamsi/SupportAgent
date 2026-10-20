"""WP-013: proposals, cases, traces, grants and users on the API (02 §2).

Canonical test IDs from 10-wp §WP-013: ``test_api_list_pending_proposals``,
``test_api_approve_happy_path_enqueues_outbox``, ``test_api_approve_stale_hash_409``,
``test_api_self_approve_403``, ``test_api_reject_requires_reason``,
``test_api_problem_json_has_trace_id``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.fakes.oidc import FakeOIDC
from tests.unit.api_support import (
    ADA,
    BOB,
    CARL,
    csrf,
    login,
    logout,
    make_client,
    seed_proposal,
    seed_users,
)


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def _as(client: TestClient, oidc: FakeOIDC, db: Any, who: str) -> None:
    """Log in as ada (admin), bob (approver) or carl (viewer)."""
    login(client, oidc, ADA)  # first login makes ada the bootstrap admin
    if who == "ada":
        return
    logout(client)
    seed_users(db, ("sub-bob", "approver"), ("sub-carl", "viewer"))
    login(client, oidc, {"bob": BOB, "carl": CARL}[who])


def _one(db: Any, sql: str) -> Any:
    with db.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_api_list_pending_proposals(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    seed_proposal(db, proposal_id="prop_low", case_id="case_low", severity="P3", created_at_ts=1)
    seed_proposal(db, proposal_id="prop_hi", case_id="case_hi", severity="P1", created_at_ts=2)
    seed_proposal(db, proposal_id="prop_done", case_id="case_done", status="approved")
    _as(client, oidc, db, "carl")  # viewer is enough to list
    resp = client.get("/api/v1/proposals", params={"status": "pending_approval"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [p["id"] for p in items] == ["prop_hi", "prop_low"], "severity first, then age"
    assert items[0]["severity"] == "P1"
    assert items[0]["payload_hash"]
    by_case = client.get("/api/v1/proposals", params={"case_id": "case_low"}).json()["items"]
    assert [p["id"] for p in by_case] == ["prop_low"]


def test_api_proposal_detail_has_payload_hash_preview_and_trace(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "carl")
    body = client.get("/api/v1/proposals/prop_1").json()
    assert body["payload_hash"] == h
    assert body["payload"]["text"] == "payments / P3 / payments/api"
    assert body["preview"] == "payments / P3 / payments/api"
    assert body["trace_url"] == "/api/v1/traces/tr_prop_1"


def test_api_approve_happy_path_enqueues_outbox(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "bob")
    resp = client.post(
        "/api/v1/proposals/prop_1/approve",
        json={"payload_hash": h, "reason": "looks right"},
        headers=csrf(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # trg_outbox_mark_executed: enqueueing consumes the approval (single-use).
    assert _one(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == "executed"
    with db.connect() as conn:
        row = (
            conn.execute(text("SELECT approval_id, status, payload_hash, scope FROM outbox"))
            .mappings()
            .one()
        )
    assert row["approval_id"] == body["approval_id"]
    assert row["status"] == "pending"
    assert row["payload_hash"] == h, "bytes enqueued = bytes approved"
    assert row["scope"] == "service:payments:work_note"
    assert body["outbox_id"]


def test_api_approve_requires_approver_role(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "carl")
    resp = client.post(
        "/api/v1/proposals/prop_1/approve", json={"payload_hash": h}, headers=csrf(client)
    )
    assert resp.status_code == 403
    assert resp.json()["title"] == "rbac"


def test_api_approve_stale_hash_409(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "bob")
    resp = client.post(
        "/api/v1/proposals/prop_1/approve",
        json={"payload_hash": "0" * 64},
        headers=csrf(client),
    )
    assert resp.status_code == 409
    assert resp.json()["title"] == "stale_proposal"
    assert _one(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == "pending_approval"
    assert _one(db, "SELECT COUNT(*) FROM outbox") == 0


def test_api_self_approve_403(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    # trg_no_self_approval: an approver may not approve a proposal they created.
    h = seed_proposal(db, proposal_id="prop_1", created_by="sub-bob")
    _as(client, oidc, db, "bob")
    resp = client.post(
        "/api/v1/proposals/prop_1/approve", json={"payload_hash": h}, headers=csrf(client)
    )
    assert resp.status_code == 403
    assert resp.json()["title"] == "self_approval"
    assert _one(db, "SELECT COUNT(*) FROM approvals") == 0
    assert _one(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == "pending_approval"


def test_api_approve_twice_conflicts(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "bob")
    first = client.post(
        "/api/v1/proposals/prop_1/approve", json={"payload_hash": h}, headers=csrf(client)
    )
    assert first.status_code == 200
    again = client.post(
        "/api/v1/proposals/prop_1/approve", json={"payload_hash": h}, headers=csrf(client)
    )
    assert again.status_code == 409
    assert _one(db, "SELECT COUNT(*) FROM outbox") == 1


@pytest.mark.parametrize("reason", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_api_reject_requires_reason(
    client: TestClient, oidc: FakeOIDC, db: Any, reason: str | None
) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "bob")
    body: dict[str, object] = {"payload_hash": h}
    if reason is not None:
        body["reason"] = reason
    resp = client.post("/api/v1/proposals/prop_1/reject", json=body, headers=csrf(client))
    assert resp.status_code == 422
    assert _one(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == "pending_approval"
    ok = client.post(
        "/api/v1/proposals/prop_1/reject",
        json={"payload_hash": h, "reason": "wrong service"},
        headers=csrf(client),
    )
    assert ok.status_code == 200
    assert _one(db, "SELECT status FROM proposals WHERE id = 'prop_1'") == "rejected"
    assert _one(db, "SELECT reason FROM approvals") == "wrong service"
    assert _one(db, "SELECT COUNT(*) FROM outbox") == 0


def test_api_problem_json_has_trace_id(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _as(client, oidc, db, "carl")
    resp = client.get("/api/v1/proposals/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert set(body) == {"type", "title", "status", "detail", "trace_id"}
    assert body["trace_id"].startswith("tr_")
    assert body["trace_id"] == resp.headers["X-Trace-Id"]


def test_api_case_view(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "carl")
    body = client.get("/api/v1/cases/case_0001").json()
    assert body["case"]["id"] == "case_0001"
    assert body["case"]["severity"] == "P3"
    assert body["decision"]["reason"] == "routed"
    assert [p["id"] for p in body["proposals"]] == ["prop_1"]
    assert body["outbox"] == []
    assert client.get("/api/v1/cases/nope").status_code == 404


def test_api_trace_replay_shows_approval(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    h = seed_proposal(db, proposal_id="prop_1")
    _as(client, oidc, db, "bob")
    client.post("/api/v1/proposals/prop_1/approve", json={"payload_hash": h}, headers=csrf(client))
    view = client.get("/api/v1/traces/tr_prop_1").json()
    gate = [e for e in view["events"] if e["kind"] == "gate"]
    assert gate and gate[-1]["actor"] == "sub-bob"
    assert "transition=approved" in gate[-1]["summary"]
    assert client.get("/api/v1/traces/tr_missing").status_code == 404


def test_api_grants_list_approve_revoke(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _as(client, oidc, db, "ada")
    client.post("/api/v1/setup/approve", headers=csrf(client))
    approved = client.get("/api/v1/grants", params={"status": "approved"}).json()["items"]
    assert len(approved) == 3
    gid = approved[0]["id"]
    resp = client.post(f"/api/v1/grants/{gid}/revoke", headers=csrf(client))
    assert resp.status_code == 200
    assert _one(db, f"SELECT status FROM policy_grants WHERE id = '{gid}'") == "revoked"  # noqa: S608
    revoked = client.get("/api/v1/grants", params={"status": "revoked"}).json()["items"]
    assert [g["id"] for g in revoked] == [gid]
    again = client.post(f"/api/v1/grants/{gid}/revoke", headers=csrf(client))
    assert again.status_code == 409


def test_api_grants_admin_only(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    _as(client, oidc, db, "bob")
    assert client.get("/api/v1/grants").status_code == 403


def test_api_assign_role(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    # BL-021: an admin gives a second user a role through the API.
    login(client, oidc, ADA)
    logout(client)
    login(client, oidc, BOB)  # no role yet
    assert client.get("/api/v1/proposals").status_code == 403
    logout(client)
    login(client, oidc, ADA)
    users = client.get("/api/v1/users").json()["items"]
    assert {u["id"] for u in users} == {"sub-ada", "sub-bob"}
    resp = client.post(
        "/api/v1/users/sub-bob/roles", json={"role": "approver"}, headers=csrf(client)
    )
    assert resp.status_code == 200
    logout(client)
    login(client, oidc, BOB)
    assert client.get("/api/v1/proposals").status_code == 200
    denied = client.post(
        "/api/v1/users/sub-bob/roles", json={"role": "admin"}, headers=csrf(client)
    )
    assert denied.status_code == 403
