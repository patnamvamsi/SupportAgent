"""WP-103: RBAC roles (ARCH §6.4, 03 §2.1).

Canonical test IDs from 11-wp §WP-103: ``test_rbac_matrix`` (every cell),
``test_approver_scope_service_limited``, ``test_role_change_emits_event``,
``test_last_admin_cannot_be_removed``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.setup import SetupService
from supportagent.domain.errors import AuthorizationDenied, DomainValidationError, NotFound
from supportagent.domain.rbac import (
    MATRIX,
    Permission,
    RoleGrant,
    can,
    service_role_scope,
    validate_scope,
)
from supportagent.domain.roles import Role
from tests.unit.api_support import (
    ADA,
    BOB,
    TENANT,
    catalog,
    csrf,
    login,
    logout,
    make_client,
    seed_proposal,
    seed_users,
)

_V, _AN, _AP, _AD = Role.VIEWER, Role.ANALYST, Role.APPROVER, Role.ADMIN
#: 11-wp WP-103's table, cell by cell.
_EXPECTED = {
    Permission.VIEW: {_V, _AN, _AP, _AD},
    Permission.HAND_BACK: {_AN, _AP, _AD},
    Permission.DECIDE_PROPOSAL: {_AP, _AD},
    Permission.ADMINISTER: {_AD},
    Permission.REVIEW_KNOWLEDGE: {_AN, _AP, _AD},
    Permission.TRACE: {_AN, _AP, _AD},
}


@pytest.mark.parametrize("permission", list(Permission))
@pytest.mark.parametrize("role", list(Role))
def test_rbac_matrix(role: Role, permission: Permission) -> None:
    expected = role in _EXPECTED[permission]
    assert can([RoleGrant(role)], permission, "service:payments") is expected
    assert (role in MATRIX[permission]) is expected


def test_scoped_grant_covers_only_its_service() -> None:
    grants = [RoleGrant(Role.APPROVER, "service:payments")]
    assert can(grants, Permission.DECIDE_PROPOSAL, "service:payments")
    assert not can(grants, Permission.DECIDE_PROPOSAL, "service:search")
    assert not can(grants, Permission.DECIDE_PROPOSAL, None), "unresolved needs '*'"
    assert can(grants, Permission.DECIDE_PROPOSAL, anywhere=True)
    assert can([RoleGrant(Role.APPROVER)], Permission.DECIDE_PROPOSAL, None)
    assert service_role_scope("payments") == "service:payments"
    assert service_role_scope(None) is None


@pytest.mark.parametrize("scope", ["", "service:", "svc:payments", "service:Pay ments"])
def test_bad_scopes_rejected(scope: str) -> None:
    with pytest.raises(DomainValidationError):
        validate_scope(Role.APPROVER, scope)


def test_admin_is_never_scope_limited() -> None:
    with pytest.raises(DomainValidationError, match="tenant-wide"):
        validate_scope(Role.ADMIN, "service:payments")


# --- the approver's scope, end to end ------------------------------------------


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc, catalog("payments", "search"))


def test_approver_scope_service_limited(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    pay = seed_proposal(db, proposal_id="prop_pay", case_id="case_pay", service="payments")
    srch = seed_proposal(
        db, proposal_id="prop_srch", case_id="case_srch", service="search", external_id="INC2"
    )
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "approver", "service:payments"))
    login(client, oidc, BOB)
    denied = client.post(
        "/api/v1/proposals/prop_srch/approve", json={"payload_hash": srch}, headers=csrf(client)
    )
    assert denied.status_code == 403
    assert "search" in denied.json()["detail"]
    rejected = client.post(
        "/api/v1/proposals/prop_srch/reject",
        json={"payload_hash": srch, "reason": "nope"},
        headers=csrf(client),
    )
    assert rejected.status_code == 403, "reject is scope-limited too"
    ok = client.post(
        "/api/v1/proposals/prop_pay/approve", json={"payload_hash": pay}, headers=csrf(client)
    )
    assert ok.status_code == 200, ok.text
    # The UI hides the decision buttons outside the approver's scope.
    assert "Approve" not in client.get("/ui/proposals/prop_srch").text


# --- role management -----------------------------------------------------------


@pytest.fixture
def setup(db: Any, clock: Any, ids: Any, events: Any) -> SetupService:
    return SetupService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments", "search"),
        tenant_id=TENANT,
    )


def test_role_change_emits_event(setup: SetupService, db: Any, events: Any) -> None:
    seed_users(db, ("sub-ada", "admin"), ("sub-bob", None))
    setup.assign_role(
        actor_id="sub-ada", user_id="sub-bob", role=Role.APPROVER, scope="service:search"
    )
    user = setup.user("sub-bob")
    assert user is not None and user.grants == {RoleGrant(Role.APPROVER, "service:search")}
    setup.assign_role(actor_id="sub-ada", user_id="sub-bob", role=Role.APPROVER)  # widen
    user = setup.user("sub-bob")
    assert user is not None and user.grants == {RoleGrant(Role.APPROVER, "*")}
    setup.revoke_role(actor_id="sub-ada", user_id="sub-bob", role=Role.APPROVER)
    user = setup.user("sub-bob")
    assert user is not None and user.grants == frozenset()
    changes = [
        (e.actor, e.payload["transition"], e.payload.get("scope"))
        for e in events.appended
        if e.payload.get("transition") in ("role_assigned", "role_revoked")
    ]
    assert changes == [
        ("sub-ada", "role_assigned", "service:search"),
        ("sub-ada", "role_assigned", "*"),
        ("sub-ada", "role_revoked", None),
    ]


def test_role_scope_must_name_a_catalog_service(setup: SetupService, db: Any) -> None:
    seed_users(db, ("sub-ada", "admin"), ("sub-bob", None))
    with pytest.raises(DomainValidationError, match="unknown service"):
        setup.assign_role(
            actor_id="sub-ada", user_id="sub-bob", role=Role.APPROVER, scope="service:billing"
        )


def test_last_admin_cannot_be_removed(setup: SetupService, db: Any) -> None:
    seed_users(db, ("sub-ada", "admin"), ("sub-bob", None))
    with pytest.raises(DomainValidationError, match="last admin"):
        setup.revoke_role(actor_id="sub-ada", user_id="sub-ada", role=Role.ADMIN)
    setup.assign_role(actor_id="sub-ada", user_id="sub-bob", role=Role.ADMIN)
    setup.revoke_role(actor_id="sub-bob", user_id="sub-ada", role=Role.ADMIN)  # now allowed
    with pytest.raises(DomainValidationError, match="last admin"):
        setup.revoke_role(actor_id="sub-bob", user_id="sub-bob", role=Role.ADMIN)
    with pytest.raises(AuthorizationDenied):
        setup.revoke_role(
            actor_id="sub-ada", user_id="sub-bob", role=Role.ADMIN
        )  # ada no longer admin
    with pytest.raises(NotFound):
        setup.revoke_role(actor_id="sub-bob", user_id="sub-ada", role=Role.VIEWER)


def test_users_page_assigns_and_revokes(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    seed_users(db, ("sub-bob", None))
    page = client.get("/ui/users")
    assert page.status_code == 200 and "service:search" in page.text
    token = client.cookies.get("sa_csrf")
    form = {"csrf_token": token, "role": "approver", "scope": "service:search"}
    done = client.post("/ui/users/sub-bob/roles", data=form, follow_redirects=False)
    assert done.status_code == 303
    users = {u["id"]: u for u in client.get("/api/v1/users").json()["items"]}
    assert users["sub-bob"]["role_scopes"] == {"approver": "service:search"}
    gone = client.post(
        "/ui/users/sub-bob/roles/approver/revoke",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert gone.status_code == 303
    users = {u["id"]: u for u in client.get("/api/v1/users").json()["items"]}
    assert users["sub-bob"]["roles"] == []
    last = client.delete("/api/v1/users/sub-ada/roles/admin", headers=csrf(client))
    assert last.status_code == 422


def test_viewer_cannot_open_users_page(client: TestClient, oidc: FakeOIDC, db: Any) -> None:
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "viewer"))
    login(client, oidc, BOB)
    assert client.get("/ui/users").status_code == 403
    assert client.get("/api/v1/users").status_code == 403
