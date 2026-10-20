"""Shared API test wiring (WP-012/013): a real app over the test DB + FakeOIDC.

``make_client`` builds the app through ``bootstrap.api_context_for`` — the same
per-request factories production uses — with the test clock, id generator and
master key. ``login``/``csrf`` drive the OIDC flow and the double-submit token.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import text

from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.bootstrap import api_context_for
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.entrypoints.api.app import create_app
from supportagent.ports.identity import IdentityClaims

TENANT = "acme"
MARKER = "config:abc123def456"
SECRET = "s" * 32

ADA = IdentityClaims(sub="sub-ada", email="ada@acme.test", name="Ada")  # first: admin
BOB = IdentityClaims(sub="sub-bob", email="bob@acme.test", name="Bob")
CARL = IdentityClaims(sub="sub-carl", email="carl@acme.test", name="Carl")


def service(name: str) -> ServiceEntry:
    return ServiceEntry(
        name=name,
        repos=[f"acme/{name}"],
        log_paths=[f"/var/log/{name}"],
        dags=[],
        cmdb_ci=[f"CI{name.upper()}"],
        resources=[f"{name}-api"],
        owners=[f"team-{name}"],
        depends_on=[],
    )


def catalog(*names: str) -> Catalog:
    return Catalog(version="1", services=[service(n) for n in names])


def make_client(  # noqa: PLR0913, PLR0917 — the test ports
    db: Any,
    clock: Any,
    ids: Any,
    master_key: bytes,
    oidc: FakeOIDC,
    cat: Catalog | None = None,
) -> TestClient:
    ctx = api_context_for(
        db,
        tenant_id=TENANT,
        catalog=cat or catalog("payments"),
        master_key=master_key,
        clock=clock,
        ids=ids,
        identity=oidc,
        session_secret=SECRET,
        secure_cookies=False,
        config_marker=MARKER,
    )
    return TestClient(create_app(ctx))


def login(client: TestClient, oidc: FakeOIDC, claims: IdentityClaims) -> None:
    start = client.get("/auth/login", follow_redirects=False)
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    code = oidc.authorize(state, claims)
    done = client.get(f"/auth/callback?code={code}&state={state}", follow_redirects=False)
    assert done.status_code == 303, done.text


def logout(client: TestClient) -> None:
    assert client.post("/auth/logout", headers=csrf(client)).status_code == 204


def csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("sa_csrf")
    assert token, "login sets the double-submit CSRF cookie"
    return {"X-CSRF-Token": token}


def seed_users(db: Any, *rows: tuple[str, str | None]) -> None:
    """Insert users with an optional role each (``(sub, role)``), admin-granted by ada."""
    with db.begin() as conn:
        for sub, role in rows:
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                    "VALUES (:id, :t, :id, :id, 1, 0) ON CONFLICT (id) DO NOTHING"
                ),
                {"id": sub, "t": TENANT},
            )
            if role is not None:
                conn.execute(
                    text(
                        "INSERT INTO role_assignments "
                        "(user_id, tenant_id, role, scope, granted_by, granted_ts) "
                        "VALUES (:id, :t, :role, '*', NULL, 0)"
                    ),
                    {"id": sub, "t": TENANT, "role": role},
                )


WORK_NOTE = {"template_id": "work_note_v1", "text": "payments / P3 / payments/api"}


def seed_proposal(  # noqa: PLR0913 — one knob per column the tests vary
    db: Any,
    *,
    proposal_id: str,
    case_id: str = "case_0001",
    severity: str = "P3",
    status: str = "pending_approval",
    created_by: str = "agent",
    created_at_ts: int = 1_000,
    payload: dict[str, object] | None = None,
    sys_id: str = "INC0000001",
) -> str:
    """A case (if new) plus one proposal with a real canonical payload hash."""
    from supportagent.domain.hashing import canonical_json, payload_hash  # noqa: PLC0415

    body = payload if payload is not None else WORK_NOTE
    trace_id = f"tr_{proposal_id}"
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts, external_table, external_id, "
                "service, severity, classification, decision_json) VALUES (:id, :t, :fp, "
                "'alert', 'triaged', :tr, 0, 0, 0, 'incident', :sys, 'payments', :sev, "
                "'payments/api', :dj) ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": case_id,
                "t": TENANT,
                "fp": f"fp_{case_id}",
                "tr": trace_id,
                "sys": sys_id,
                "sev": severity,
                "dj": '{"outcome": "routed", "reason": "routed"}',
            },
        )
        conn.execute(
            text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, "
                "payload_json, payload_hash, trace_id, status, created_by, created_at_ts, "
                "expires_ts) VALUES (:id, :t, :case, 'work_note', 'service:payments:work_note', "
                ":pj, :ph, :tr, :status, :by, :at, 9999999999)"
            ),
            {
                "id": proposal_id,
                "t": TENANT,
                "case": case_id,
                "pj": canonical_json(body),
                "ph": payload_hash(body),
                "tr": trace_id,
                "status": status,
                "by": created_by,
                "at": created_at_ts,
            },
        )
    return payload_hash(body)
