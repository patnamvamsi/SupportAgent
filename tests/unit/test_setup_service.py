"""WP-012: ``SetupService`` — first admin, default T1 grants, batch approve.

Canonical test IDs from 10-wp §WP-012: ``test_first_user_becomes_admin_once``,
``test_setup_requests_default_t1_grants_per_service``,
``test_setup_batch_approve_sets_anchored_expiry``,
``test_grant_approver_cannot_be_requester``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.setup import SetupService
from supportagent.domain.errors import AuthenticationFailed, AuthorizationDenied
from supportagent.domain.events import EventKind
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.roles import Role
from supportagent.ports.identity import IdentityClaims

TENANT = "acme"
MARKER = "config:abc123def456"
ADA = IdentityClaims(sub="sub-ada", email="ada@acme.test", name="Ada")
BOB = IdentityClaims(sub="sub-bob", email="bob@acme.test", name="Bob")


def _service(name: str) -> ServiceEntry:
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


def _catalog(*names: str) -> Catalog:
    return Catalog(version="1", services=[_service(n) for n in names])


@pytest.fixture
def setup(db: Any, clock: Any, ids: Any, events: Any) -> SetupService:
    return SetupService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        catalog=_catalog("payments", "search"),
        tenant_id=TENANT,
    )


def _rows(db: Any, sql: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql)).mappings()]


def test_first_user_becomes_admin_once(setup: SetupService, db: Any, events: Any) -> None:
    # 03 §2.1: the first login with no admin creates `admin`, recorded as an event.
    ada = setup.login(ADA)
    assert ada.roles == frozenset({Role.ADMIN})
    bob = setup.login(BOB)
    assert bob.roles == frozenset(), "only the first user becomes admin"
    again = setup.login(ADA)
    assert again.roles == frozenset({Role.ADMIN})
    admins = _rows(db, "SELECT user_id FROM role_assignments WHERE role = 'admin'")
    assert admins == [{"user_id": "sub-ada"}]
    first_admin = [e for e in events.appended if e.payload.get("transition") == "first_admin"]
    assert len(first_admin) == 1
    assert first_admin[0].kind == EventKind.GATE
    assert first_admin[0].payload["user_id"] == "sub-ada"


def test_login_refused_for_inactive_user(setup: SetupService, db: Any) -> None:
    setup.login(ADA)
    with db.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE id = 'sub-ada'"))
    with pytest.raises(AuthenticationFailed):
        setup.login(ADA)
    assert setup.user("sub-ada") is None


def test_setup_requests_default_t1_grants_per_service(setup: SetupService, db: Any) -> None:
    # ARCH §6.4 / ADR-0005: one requested grant per (catalog service x T1 kind),
    # literal `service:<name>:<kind>`, requested_by = config:<sha>.
    created = setup.request_default_grants(requested_by=MARKER)
    assert len(created) == 6
    rows = _rows(db, "SELECT action_kind, scope, status, requested_by FROM policy_grants")
    expected = {
        (kind.value, f"service:{svc}:{kind.value}")
        for svc in ("payments", "search")
        for kind in (
            ActionKind.WORK_NOTE,
            ActionKind.ENRICH_PAGE,
            ActionKind.WATCH_LIST_ADD_INTERNAL,
        )
    }
    assert {(r["action_kind"], r["scope"]) for r in rows} == expected
    assert {r["status"] for r in rows} == {"requested"}
    assert {r["requested_by"] for r in rows} == {MARKER}
    # Idempotent: a second request writes nothing new.
    assert setup.request_default_grants(requested_by=MARKER) == []
    assert len(_rows(db, "SELECT id FROM policy_grants")) == 6
    state = setup.state()
    assert state.has_admin is False
    assert len(state.default_grants) == 6
    assert all(g.status == "requested" for g in state.default_grants)


def test_setup_batch_approve_sets_anchored_expiry(setup: SetupService, db: Any, clock: Any) -> None:
    # 10-wp WP-012: one batch sets granted_by, valid_to_ts = now + 365d, approved.
    ada = setup.login(ADA)
    setup.request_default_grants(requested_by=MARKER)
    clock.advance(3600)
    now_ts = int(clock.now().timestamp())
    approved = setup.approve_default_grants(approver_id=ada.id)
    assert len(approved) == 6
    rows = _rows(db, "SELECT status, granted_by, granted_at_ts, valid_to_ts FROM policy_grants")
    assert {r["status"] for r in rows} == {"approved"}
    assert {r["granted_by"] for r in rows} == {"sub-ada"}
    assert {r["granted_at_ts"] for r in rows} == {now_ts}
    year = int(timedelta(days=365).total_seconds())
    assert {r["valid_to_ts"] for r in rows} == {now_ts + year}, "anchored at approval time"
    assert setup.approve_default_grants(approver_id=ada.id) == []


def test_grant_approver_cannot_be_requester(setup: SetupService, db: Any) -> None:
    # ARCH §6.4: CHECK granted_by <> requested_by; the service maps it to
    # AuthorizationDenied(self_approval), which the API maps to 403.
    ada = setup.login(ADA)
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_grants "
                "(id, tenant_id, action_kind, scope, status, requested_by, requested_at_ts) "
                "VALUES ('grnt_self', 'acme', 'work_note', 'service:payments:work_note', "
                "'requested', 'sub-ada', 0)"
            )
        )
    with pytest.raises(AuthorizationDenied) as info:
        setup.approve_grant("grnt_self", approver_id=ada.id, valid_days=30)
    assert info.value.code == "self_approval"
    rows = _rows(db, "SELECT status, granted_by FROM policy_grants")
    assert rows == [{"status": "requested", "granted_by": None}]


def test_assign_role_requires_admin(setup: SetupService) -> None:
    # 03 §2.1: every role change after the first admin is made by an admin.
    ada = setup.login(ADA)
    bob = setup.login(BOB)
    with pytest.raises(AuthorizationDenied) as info:
        setup.assign_role(actor_id=bob.id, user_id=bob.id, role=Role.APPROVER)
    assert info.value.code == "rbac"
    setup.assign_role(actor_id=ada.id, user_id=bob.id, role=Role.APPROVER)
    refreshed = setup.user(bob.id)
    assert refreshed is not None
    assert refreshed.roles == frozenset({Role.APPROVER})
    assert refreshed.has_role(Role.VIEWER), "roles are ranked: approver implies viewer"
    assert not refreshed.has_role(Role.ADMIN)
