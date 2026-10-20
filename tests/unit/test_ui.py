"""WP-013: the server-rendered admin UI (02 §3).

Canonical test IDs from 10-wp §WP-013:
``test_ui_proposal_form_hash_matches_rendered_payload``, ``test_ui_csp_header_present``.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.domain.hashing import payload_hash
from tests.unit.api_support import (
    ADA,
    BOB,
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


def _as_bob(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "approver"))
    login(client, oidc, BOB)


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', page)
    assert match, f"form field {name!r} missing"
    return html.unescape(match.group(1))


def test_ui_proposal_form_hash_matches_rendered_payload(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    # 02 §3: the payload shown on the approve form is the one whose hash is submitted.
    payload: dict[str, object] = {"template_id": "work_note_v1", "text": '<b>payments</b> & "P3"'}
    seed_proposal(db, proposal_id="prop_1", payload=payload)
    _as_bob(client, oidc, db)
    page = client.get("/ui/proposals/prop_1").text
    raw = re.search(r'<pre id="payload-raw">(.*?)</pre>', page, re.DOTALL)
    assert raw, "the raw payload JSON is rendered"
    shown = json.loads(html.unescape(raw.group(1)))
    assert shown == payload
    assert _field(page, "payload_hash") == payload_hash(shown)
    assert "<b>payments</b>" not in page, "payload text is escaped, never rendered as HTML"


def test_ui_approve_via_form(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    seed_proposal(db, proposal_id="prop_1")
    _as_bob(client, oidc, db)
    page = client.get("/ui/proposals/prop_1").text
    form = {
        "csrf_token": _field(page, "csrf_token"),
        "payload_hash": _field(page, "payload_hash"),
        "reason": "ok",
    }
    resp = client.post("/ui/proposals/prop_1/approve", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/proposals/prop_1"
    with db.connect() as conn:
        status = conn.execute(text("SELECT status FROM proposals")).scalar_one()
        outbox = conn.execute(text("SELECT COUNT(*) FROM outbox")).scalar_one()
    assert (status, outbox) == ("executed", 1)  # the approval is consumed on enqueue


def test_ui_reject_form_without_reason_shows_error(
    client: TestClient, oidc: FakeOIDC, db: Any
) -> None:
    seed_proposal(db, proposal_id="prop_1")
    _as_bob(client, oidc, db)
    page = client.get("/ui/proposals/prop_1").text
    form = {"csrf_token": _field(page, "csrf_token"), "payload_hash": _field(page, "payload_hash")}
    resp = client.post("/ui/proposals/prop_1/reject", data=form)
    assert resp.status_code == 422
    assert "reason is required" in resp.text


def test_ui_csp_header_present(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    for path in ("/ui/login", "/healthz", "/api/v1/proposals"):
        resp = client.get(path)
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp, path
        assert "script-src 'self'" in csp, path
        assert "frame-ancestors 'none'" in csp, path
        assert "'unsafe-inline'" not in csp, path
        assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_ui_redirects_to_login_when_signed_out(client: TestClient) -> None:
    resp = client.get("/ui/approvals", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login"
    assert "Sign in" in client.get("/ui/login").text


_UI_POSTS = [
    "/ui/proposals/prop_1/approve",
    "/ui/proposals/prop_1/reject",
    "/ui/setup/approve",
    "/ui/grants/grnt_x/approve",
    "/ui/grants/grnt_x/revoke",
]


@pytest.mark.parametrize("path", _UI_POSTS)
def test_ui_post_requires_csrf_field(
    client: TestClient, oidc: FakeOIDC, db: Any, path: str
) -> None:
    seed_proposal(db, proposal_id="prop_1")
    login(client, oidc, ADA)
    missing = client.post(path, data={"payload_hash": "x", "reason": "r"})
    assert missing.status_code == 403, path
    wrong = client.post(path, data={"csrf_token": "forged", "payload_hash": "x"})
    assert wrong.status_code == 403, path


def test_ui_pages_render_for_admin(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    seed_proposal(db, proposal_id="prop_1", severity="P1")
    login(client, oidc, ADA)
    approvals = client.get("/ui/approvals").text
    assert "prop_1" in approvals and "P1" in approvals
    case = client.get("/ui/cases/case_0001").text
    assert "routed" in case and "prop_1" in case
    setup = client.get("/ui/setup").text
    assert "service:payments:work_note" in setup
    page = client.get("/ui/setup").text
    resp = client.post(
        "/ui/setup/approve",
        data={"csrf_token": _field(page, "csrf_token")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    grants = client.get("/ui/grants").text
    assert grants.count("service:payments:") == 3
    assert "<h2>Approved</h2>" in grants
    assert "None." in grants  # nothing requested or revoked any more
