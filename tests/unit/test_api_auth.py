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


def test_readyz_reports_the_version_and_schema_head(client: TestClient) -> None:
    import supportagent  # noqa: PLC0415

    body = client.get("/readyz").json()
    assert body["status"] == "ready"
    assert body["version"] == supportagent.__version__
    assert body["revision"]


def test_readyz_is_503_until_migrated(client: TestClient) -> None:
    from supportagent.domain.errors import ExternalServiceError  # noqa: PLC0415

    class _Behind:
        def __init__(self, current: str | None, *, down: bool = False) -> None:
            self._current, self._down = current, down

        def head_revision(self) -> str:
            return "0022"

        def current_revision(self) -> str | None:
            if self._down:
                raise ExternalServiceError("database unreachable: refused")  # noqa: TRY003
            return self._current

        def foreign_keys_enforced(self) -> bool | None:
            return None

    import dataclasses  # noqa: PLC0415

    from supportagent.entrypoints.api.app import create_app  # noqa: PLC0415

    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    for probe, reason in (
        (_Behind("0021"), "database at 0021, this build at 0022: run migrate"),
        (_Behind(None), "database at no schema, this build at 0022: run migrate"),
        (_Behind(None, down=True), "database unreachable"),
    ):
        behind = TestClient(create_app(dataclasses.replace(ctx, schema=probe)))
        resp = behind.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["reason"] == reason
        assert "refused" not in resp.text  # no driver detail on an unauthenticated route
