"""WP-012: OIDC login, session, CSRF and RBAC on the API (02 §2).

Canonical test IDs from 10-wp §WP-012: ``test_oidc_login_creates_user``,
``test_csrf_required_on_post``, ``test_require_role_denies``; plus the API
half of ``test_grant_approver_cannot_be_requester`` (the CHECK maps to 403).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.fakes.oidc import FakeOIDC
from tests.unit.api_support import ADA, BOB, csrf, login, make_client


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def test_oidc_login_creates_user(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    with db.connect() as conn:
        row = conn.execute(text("SELECT * FROM users")).mappings().one()
    assert (row["id"], row["email"], row["name"]) == ("sub-ada", "ada@acme.test", "Ada")
    assert row["last_login_ts"] is not None
    # The session now authenticates API calls.
    assert client.get("/api/v1/setup").status_code == 200


def test_oidc_callback_rejects_state_mismatch(client: TestClient, oidc: FakeOIDC) -> None:
    start = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    code = oidc.authorize(state, ADA)
    bad = client.get(f"/auth/callback?code={code}&state=forged", follow_redirects=False)
    assert bad.status_code == 401
    assert bad.headers["content-type"].startswith("application/problem+json")


def test_csrf_required_on_post(client: TestClient, oidc: FakeOIDC) -> None:
    login(client, oidc, ADA)
    missing = client.post("/api/v1/setup/approve")
    assert missing.status_code == 403
    assert missing.json()["title"] == "csrf_failed"
    wrong = client.post("/api/v1/setup/approve", headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    ok = client.post("/api/v1/setup/approve", headers=csrf(client))
    assert ok.status_code == 200
    assert len(ok.json()["approved"]) == 3  # 1 service x 3 T1 kinds
    # Logout is a POST too, so it needs the token.
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers=csrf(client)).status_code == 204
    assert client.get("/api/v1/setup").status_code == 401


def test_require_role_denies(client: TestClient, oidc: FakeOIDC) -> None:
    assert client.get("/api/v1/setup").status_code == 401, "no session"
    login(client, oidc, ADA)  # first user: admin
    client.post("/auth/logout", headers=csrf(client))
    login(client, oidc, BOB)  # no role
    denied = client.get("/api/v1/setup")
    assert denied.status_code == 403
    body = denied.json()
    assert body["status"] == 403
    assert body["trace_id"]


def test_grant_self_approval_maps_to_403(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_grants "
                "(id, tenant_id, action_kind, scope, status, requested_by, requested_at_ts) "
                "VALUES ('grnt_self', 'acme', 'work_note', 'service:payments:work_note', "
                "'requested', 'sub-ada', 0)"
            )
        )
    resp = client.post(
        "/api/v1/grants/grnt_self/approve", json={"valid_days": 30}, headers=csrf(client)
    )
    assert resp.status_code == 403
    assert resp.json()["title"] == "self_approval"


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
