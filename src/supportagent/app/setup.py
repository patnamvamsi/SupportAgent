"""SetupService (10-wp WP-012): identities, the first admin, and setup grants.

- ``login`` upserts the OIDC user (``users.id`` is the ``sub``, ARCH §6.4). The
  first login with no admin creates ``admin`` once, recorded as a ``gate``
  event; the partial unique index ``ux_role_assignments_bootstrap_admin``
  (migration 0005) makes "once" hold under concurrent first logins.
- ``request_default_grants`` writes one ``requested`` grant per
  (catalog service x default T1 kind), scope ``service:<name>:<kind>``
  (ADR-0005), ``requested_by='config:<sha>'``. Requested rows are not
  enforceable: the gate trigger requires ``status='approved'``.
- ``approve_default_grants`` / ``approve_grant`` set ``granted_by``,
  ``granted_at_ts`` and ``valid_to_ts`` anchored at approval time. The table
  CHECK ``granted_by <> requested_by`` is the enforcement; its violation is
  mapped to ``AuthorizationDenied(code='self_approval')`` (API 403).
- ``expand_grant_requests`` turns ``grants_request`` config entries into
  literal scopes, expanding a pattern against the catalog (ARCH §6.4: there is
  no pattern matching in the enforcement path).

Like ``GateService`` it drives a structural UoW protocol and runs its SQL on
the UoW's connection (``app`` may not import ``supportagent.adapters``).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Protocol, Self

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from supportagent.domain.auto import REVERTS, SOP_SCOPE_PREFIX, AutoConditions
from supportagent.domain.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    ConcurrencyConflict,
    ConfigError,
    DomainValidationError,
    NotFound,
)
from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.event import Event
from supportagent.domain.rbac import Permission, RoleGrant, can, validate_scope
from supportagent.domain.roles import Role, satisfies
from supportagent.domain.scopes import DEFAULT_T1_KINDS, service_scope
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.identity import IdentityClaims
from supportagent.ports.idgen import IdGenerator

if TYPE_CHECKING:
    from supportagent.config.schema import GrantRequest

#: ARCH §6.4 / 10-wp WP-012: setup grants are valid for a year from approval (prov.).
DEFAULT_GRANT_VALIDITY = timedelta(days=365)

#: The CHECK that makes a grant's approver differ from its requester (03 §2.3).
_SELF_APPROVAL_CHECK = "ck_policy_grants_no_self_approval"

#: ``grants_request`` target field → the catalog list a pattern expands over.
_CATALOG_TARGETS: dict[str, str] = {
    "dag": "dags",
    "repo": "repos",
    "ci": "cmdb_ci",
    "resource": "resources",
    "log": "log_paths",
}

_GLOB_CHARS = frozenset("*?[")

GrantStatus = Literal["missing", "requested", "approved", "revoked", "expired"]


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


_ADMINS = "SELECT user_id FROM role_assignments WHERE tenant_id = :tenant AND role = 'admin'"
_ADMINS_FOR_UPDATE = _ADMINS + " FOR UPDATE"


@dataclass(frozen=True, slots=True)
class SessionUser:
    """An authenticated, active user and the roles they hold in this tenant."""

    id: str
    tenant_id: str
    email: str
    name: str
    roles: frozenset[Role]
    grants: frozenset[RoleGrant] = frozenset()

    def has_role(self, role: Role) -> bool:
        """True when a held role ranks at or above ``role`` (02 §2)."""
        return satisfies(self.roles, role)

    def can(
        self, permission: Permission, scope: str | None = None, *, anywhere: bool = False
    ) -> bool:
        """The WP-103 permission check over this user's scoped role grants."""
        return can(self.grants, permission, scope, anywhere=anywhere)


@dataclass(frozen=True, slots=True)
class DefaultGrant:
    """One expected first-run grant and the state of its newest row."""

    kind: ActionKind
    scope: str
    status: GrantStatus
    grant_id: str | None


@dataclass(frozen=True, slots=True)
class SetupState:
    """First-run state for ``GET /api/v1/setup`` (02 §2)."""

    has_admin: bool
    default_grants: list[DefaultGrant]


def expand_grant_requests(
    requests: Iterable[GrantRequest], catalog: Catalog
) -> list[tuple[ActionKind, str]]:
    """Literal ``(kind, scope)`` pairs for ``grants_request`` entries (ARCH §6.4).

    A target is ``<field>:<name>[:<rest>]``. When ``<name>`` is a glob it is
    expanded over the catalog list for ``<field>`` (``dag`` → ``dags``,
    ``repo`` → ``repos``, …), one literal scope per match, sorted; ``<rest>``
    stays literal. A pattern matching nothing, an unknown field, a glob outside
    the name, or an unknown action kind is a ``ConfigError`` — a request must
    never silently grant less (or more) than it says.
    """
    out: list[tuple[ActionKind, str]] = []
    for req in requests:
        try:
            kind = ActionKind(req.action)
        except ValueError:
            raise ConfigError(f"grants_request: unknown action {req.action!r}") from None  # noqa: TRY003
        field, _, remainder = req.target.partition(":")
        name, sep, rest = remainder.partition(":")
        if _GLOB_CHARS & set(field + rest):
            raise ConfigError(  # noqa: TRY003
                f"grants_request {req.target!r}: only the target name may be a pattern"
            )
        if not _GLOB_CHARS & set(name):
            out.append((kind, f"{req.system}:{req.target}"))
            continue
        attr = _CATALOG_TARGETS.get(field)
        if attr is None:
            raise ConfigError(  # noqa: TRY003
                f"grants_request {req.target!r}: cannot expand a pattern over {field!r} "
                f"(known: {sorted(_CATALOG_TARGETS)})"
            )
        candidates = {value for service in catalog.services for value in getattr(service, attr)}
        matches = sorted(c for c in candidates if fnmatch.fnmatchcase(c, name))
        if not matches:
            raise ConfigError(  # noqa: TRY003
                f"grants_request {req.target!r} matches no catalog {attr}"
            )
        out.extend(sorted((kind, f"{req.system}:{field}:{match}{sep}{rest}") for match in matches))
    return list(dict.fromkeys(out))


class SetupService:
    """Identities, the first admin, role changes, and grant requests/approvals."""

    def __init__(  # noqa: PLR0913 — the standard ports plus catalog and tenant
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        catalog: Catalog,
        tenant_id: str,
        grant_validity: timedelta = DEFAULT_GRANT_VALIDITY,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._catalog = catalog
        self._tenant = tenant_id
        self._validity = grant_validity

    # --- identity -----------------------------------------------------------

    def login(self, claims: IdentityClaims) -> SessionUser:
        """Upsert the OIDC user; the first login with no admin becomes admin once."""
        now = self._clock.now()
        now_ts = int(now.timestamp())
        with self._uow:
            conn = self._uow.connection
            existing = (
                conn.execute(
                    sa.text("SELECT tenant_id, is_active FROM users WHERE id = :id"),
                    {"id": claims.sub},
                )
                .mappings()
                .first()
            )
            if existing is not None and existing["tenant_id"] != self._tenant:
                raise AuthenticationFailed("this identity belongs to another tenant")  # noqa: TRY003
            if existing is not None and not existing["is_active"]:
                raise AuthenticationFailed("this user is deactivated")  # noqa: TRY003
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts, "
                    "last_login_ts) VALUES (:id, :tenant, :email, :name, 1, :now, :now) "
                    "ON CONFLICT (id) DO UPDATE SET email = excluded.email, "
                    "name = excluded.name, last_login_ts = excluded.last_login_ts"
                ),
                {
                    "id": claims.sub,
                    "tenant": self._tenant,
                    "email": claims.email,
                    "name": claims.name,
                    "now": now_ts,
                },
            )
            if not self._has_admin(conn):
                created = conn.execute(
                    sa.text(
                        "INSERT INTO role_assignments "
                        "(user_id, tenant_id, role, scope, granted_by, granted_ts) "
                        "VALUES (:uid, :tenant, 'admin', '*', NULL, :now) "
                        "ON CONFLICT (tenant_id) WHERE role = 'admin' AND granted_by IS NULL "
                        "DO NOTHING RETURNING user_id"
                    ),
                    {"uid": claims.sub, "tenant": self._tenant, "now": now_ts},
                ).first()
                if created is not None:
                    self._emit(
                        actor=claims.sub,
                        payload={"v": 1, "transition": "first_admin", "user_id": claims.sub},
                        ts=now,
                    )
            user = self._load_user(conn, claims.sub)
            self._uow.commit()
        if user is None:  # defensive: the row was upserted above
            raise AuthenticationFailed("login did not persist the user")  # noqa: TRY003
        return user

    def user(self, user_id: str) -> SessionUser | None:
        """The active user with ``user_id`` in this tenant, with fresh roles."""
        with self._uow:
            return self._load_user(self._uow.connection, user_id)

    def assign_role(self, *, actor_id: str, user_id: str, role: Role, scope: str = "*") -> None:
        """Grant ``role`` over ``scope`` to ``user_id``; only an admin may (03 §2.1).

        Re-assigning a held role changes its scope. Service scopes must name a
        catalog service.
        """
        validate_scope(role, scope)
        if scope != "*" and scope.removeprefix("service:") not in {
            s.name for s in self._catalog.services
        }:
            raise DomainValidationError(f"unknown service in scope {scope!r}")  # noqa: TRY003
        now = self._clock.now()
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, actor_id)
            if self._load_user(conn, user_id) is None:
                raise NotFound(f"user not found: {user_id!r}")  # noqa: TRY003
            if role is not Role.ADMIN:
                self._guard_last_admin(conn, user_id, losing=role)
            conn.execute(
                sa.text(
                    "INSERT INTO role_assignments "
                    "(user_id, tenant_id, role, scope, granted_by, granted_ts) "
                    "VALUES (:uid, :tenant, :role, :scope, :actor, :now) "
                    "ON CONFLICT (user_id, tenant_id, role) DO UPDATE "
                    "SET scope = excluded.scope, granted_by = excluded.granted_by, "
                    "granted_ts = excluded.granted_ts"
                ),
                {
                    "uid": user_id,
                    "tenant": self._tenant,
                    "role": role.value,
                    "scope": scope,
                    "actor": actor_id,
                    "now": int(now.timestamp()),
                },
            )
            self._emit(
                actor=actor_id,
                payload={
                    "v": 1,
                    "transition": "role_assigned",
                    "user_id": user_id,
                    "role": role.value,
                    "scope": scope,
                },
                ts=now,
            )
            self._uow.commit()

    def revoke_role(self, *, actor_id: str, user_id: str, role: Role) -> None:
        """Remove ``role`` from ``user_id``; the tenant's last admin can't be removed."""
        now = self._clock.now()
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, actor_id)
            if role is Role.ADMIN:
                self._guard_last_admin(conn, user_id, losing=role)
            deleted = conn.execute(
                sa.text(
                    "DELETE FROM role_assignments "
                    "WHERE user_id = :uid AND tenant_id = :tenant AND role = :role"
                ),
                {"uid": user_id, "tenant": self._tenant, "role": role.value},
            ).rowcount
            if not deleted:
                raise NotFound(  # noqa: TRY003
                    f"user {user_id!r} does not hold the {role.value} role"
                )
            self._emit(
                actor=actor_id,
                payload={
                    "v": 1,
                    "transition": "role_revoked",
                    "user_id": user_id,
                    "role": role.value,
                },
                ts=now,
            )
            self._uow.commit()

    def _guard_last_admin(self, conn: sa.Connection, user_id: str, *, losing: Role) -> None:
        """Refuse a change that would leave the tenant with no admin (11-wp WP-103).

        On Postgres the admin rows are locked first, so two admins removing
        each other concurrently can't both succeed; SQLite serializes writers.
        """
        if losing is not Role.ADMIN:
            return
        sql = _ADMINS_FOR_UPDATE if conn.dialect.name == "postgresql" else _ADMINS
        admins = set(conn.execute(sa.text(sql), {"tenant": self._tenant}).scalars())
        if admins == {user_id}:
            raise DomainValidationError(  # noqa: TRY003
                "the last admin can't be removed; make someone else admin first"
            )

    # --- automation actors (ARCH §8.2) ---------------------------------------

    def automation_actors(self) -> list[dict[str, object]]:
        """Candidates from ``doctor --discover-automation`` and the admin's decisions."""
        with self._uow:
            rows = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT sys_user, kind, confirmed_by, confirmed_ts FROM automation_actors "
                        "WHERE tenant_id = :tenant ORDER BY sys_user"
                    ),
                    {"tenant": self._tenant},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def decide_automation_actor(self, *, actor_id: str, sys_user: str, automation: bool) -> None:
        """Confirm a candidate as automation, or mark it a human (``kind = 'human'``).

        Only confirmed automation actors are excluded from takeover detection;
        an undecided candidate counts as human (WP-101).
        """
        now = self._clock.now()
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, actor_id)
            row = conn.execute(
                sa.text(
                    "SELECT kind FROM automation_actors WHERE tenant_id = :tenant AND sys_user = :u"
                ),
                {"tenant": self._tenant, "u": sys_user},
            ).first()
            if row is None:
                raise NotFound(f"no automation-actor candidate {sys_user!r}")  # noqa: TRY003
            kind = (str(row[0]) if row[0] != "human" else "integration") if automation else "human"
            conn.execute(
                sa.text(
                    "UPDATE automation_actors SET kind = :kind, confirmed_by = :by, "
                    "confirmed_ts = :ts WHERE tenant_id = :tenant AND sys_user = :u"
                ),
                {
                    "kind": kind,
                    "by": actor_id,
                    "ts": int(now.timestamp()),
                    "tenant": self._tenant,
                    "u": sys_user,
                },
            )
            self._emit(
                actor=actor_id,
                payload={
                    "v": 1,
                    "transition": "automation_confirmed" if automation else "automation_rejected",
                    "sys_user": sys_user,
                },
                ts=now,
            )
            self._uow.commit()

    def service_names(self) -> list[str]:
        """The catalog's services: the scopes a role can be limited to."""
        return sorted(s.name for s in self._catalog.services)

    # --- setup grants -------------------------------------------------------

    def default_grant_literals(self) -> list[tuple[ActionKind, str]]:
        """One ``(kind, service:<name>:<kind>)`` per catalog service x T1 kind."""
        return [
            (kind, service_scope(service.name, kind))
            for service in self._catalog.services
            for kind in DEFAULT_T1_KINDS
        ]

    def state(self) -> SetupState:
        """Whether an admin exists, and the status of every default T1 grant."""
        now_ts = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            has_admin = self._has_admin(conn)
            grants = [
                self._default_grant(conn, kind, scope, now_ts)
                for kind, scope in self.default_grant_literals()
            ]
        return SetupState(has_admin=has_admin, default_grants=grants)

    def request_default_grants(self, *, requested_by: str) -> list[str]:
        """Write the missing default T1 grant requests (ARCH §6.4); idempotent."""
        return self.request_grants(self.default_grant_literals(), requested_by=requested_by)

    def request_grants(
        self, literals: Sequence[tuple[ActionKind, str]], *, requested_by: str
    ) -> list[str]:
        """Write a ``requested`` row per literal not already requested or in force."""
        now_ts = int(self._clock.now().timestamp())
        created: list[str] = []
        with self._uow:
            conn = self._uow.connection
            for kind, scope in literals:
                live = conn.execute(
                    sa.text(
                        "SELECT 1 AS one FROM policy_grants WHERE tenant_id = :tenant "
                        "AND action_kind = :kind AND scope = :scope AND (status = 'requested' "
                        "OR (status = 'approved' AND valid_to_ts > :now))"
                    ),
                    {"tenant": self._tenant, "kind": kind.value, "scope": scope, "now": now_ts},
                ).first()
                if live is not None:
                    continue
                grant_id = self._ids.new(IdPrefix.GRANT)
                conn.execute(
                    sa.text(
                        "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                        "requested_by, requested_at_ts) VALUES (:id, :tenant, :kind, :scope, "
                        "'requested', :by, :now)"
                    ),
                    {
                        "id": grant_id,
                        "tenant": self._tenant,
                        "kind": kind.value,
                        "scope": scope,
                        "by": requested_by,
                        "now": now_ts,
                    },
                )
                created.append(grant_id)
            self._uow.commit()
        return created

    def request_sop_auto(self, scope: str, conditions: AutoConditions, *, requested_by: str) -> str:
        """ADR-0014 part 2: request an auto-mode grant on one SOP step's scope.

        The kind comes from the scope and must declare a revert. The grant does
        nothing until an admin approves it in ``/ui/grants``, which shows its
        conditions; one live request or grant per scope.
        """
        prefix, _, kind_value = scope.rpartition(":")
        if not scope.startswith(SOP_SCOPE_PREFIX) or not prefix:
            raise DomainValidationError(f"not an SOP step scope: {scope!r}")  # noqa: TRY003
        try:
            kind = ActionKind(kind_value)
        except ValueError:
            raise DomainValidationError(f"unknown kind in {scope!r}") from None  # noqa: TRY003
        if kind not in REVERTS:
            raise DomainValidationError(  # noqa: TRY003
                f"{kind.value} declares no revert, so it can't run in auto mode"
            )
        known = {s.name for s in self._catalog.services}
        unknown = sorted(set(conditions.services) - known)
        if unknown:
            raise DomainValidationError(f"unknown service(s): {unknown}")  # noqa: TRY003
        now_ts = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            live = conn.execute(
                sa.text(
                    "SELECT id FROM policy_grants WHERE tenant_id = :tenant AND scope = :scope "
                    "AND (status = 'requested' OR (status = 'approved' AND valid_to_ts > :now))"
                ),
                {"tenant": self._tenant, "scope": scope, "now": now_ts},
            ).first()
            if live is not None:
                raise ConcurrencyConflict(  # noqa: TRY003
                    f"{scope!r} already has grant {live[0]!r}; revoke it to change the conditions"
                )
            grant_id = self._ids.new(IdPrefix.GRANT)
            conn.execute(
                sa.text(
                    "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                    "requested_by, requested_at_ts, conditions_json) VALUES (:id, :tenant, "
                    ":kind, :scope, 'requested', :by, :now, :conditions)"
                ),
                {
                    "id": grant_id,
                    "tenant": self._tenant,
                    "kind": kind.value,
                    "scope": scope,
                    "by": requested_by,
                    "now": now_ts,
                    "conditions": conditions.to_json(),
                },
            )
            self._uow.commit()
        return grant_id

    def approve_default_grants(self, *, approver_id: str) -> list[str]:
        """Approve every requested default T1 grant in one batch (10-wp WP-012)."""
        now = self._clock.now()
        approved: list[str] = []
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, approver_id)
            for kind, scope in self.default_grant_literals():
                rows = conn.execute(
                    sa.text(
                        "SELECT id FROM policy_grants WHERE tenant_id = :tenant "
                        "AND action_kind = :kind AND scope = :scope AND status = 'requested' "
                        "AND requested_by LIKE 'config:%' ORDER BY requested_at_ts, id"
                    ),
                    {"tenant": self._tenant, "kind": kind.value, "scope": scope},
                ).all()
                for (grant_id,) in rows:
                    self._approve(conn, grant_id, approver_id, now, self._validity)
                    approved.append(grant_id)
            self._uow.commit()
        return approved

    def run_setup(self, *, approver_id: str, requested_by: str) -> list[str]:
        """``POST /api/v1/setup/approve``: request the missing defaults, approve all, one UoW."""
        with self._uow:
            self.request_default_grants(requested_by=requested_by)
            approved = self.approve_default_grants(approver_id=approver_id)
            self._uow.commit()
        return approved

    def approve_grant(self, grant_id: str, *, approver_id: str, valid_days: int) -> None:
        """Approve one requested grant (``POST /api/v1/grants/{id}/approve``, 02 §2)."""
        now = self._clock.now()
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, approver_id)
            row = (
                conn.execute(
                    sa.text(
                        "SELECT status FROM policy_grants WHERE id = :id AND tenant_id = :tenant"
                    ),
                    {"id": grant_id, "tenant": self._tenant},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound(f"grant not found: {grant_id!r}")  # noqa: TRY003
            if row["status"] != "requested":
                raise ConcurrencyConflict(  # noqa: TRY003
                    f"grant {grant_id!r} is {row['status']!r}, not 'requested'"
                )
            self._approve(conn, grant_id, approver_id, now, timedelta(days=valid_days))
            self._uow.commit()

    def revoke_grant(self, grant_id: str, *, actor_id: str) -> None:
        """Revoke a requested or approved grant (``POST /api/v1/grants/{id}/revoke``)."""
        now = self._clock.now()
        with self._uow:
            conn = self._uow.connection
            self._require_admin(conn, actor_id)
            result = conn.execute(
                sa.text(
                    "UPDATE policy_grants SET status = 'revoked' WHERE id = :id "
                    "AND tenant_id = :tenant AND status IN ('requested', 'approved')"
                ),
                {"id": grant_id, "tenant": self._tenant},
            )
            if result.rowcount != 1:
                exists = conn.execute(
                    sa.text("SELECT 1 FROM policy_grants WHERE id = :id AND tenant_id = :t"),
                    {"id": grant_id, "t": self._tenant},
                ).first()
                if exists is None:
                    raise NotFound(f"grant not found: {grant_id!r}")  # noqa: TRY003
                raise ConcurrencyConflict(f"grant {grant_id!r} is not revocable")  # noqa: TRY003
            self._emit(
                actor=actor_id,
                payload={"v": 1, "transition": "grant_revoked", "grant_id": grant_id},
                ts=now,
            )
            self._uow.commit()

    # --- helpers ------------------------------------------------------------

    def _approve(
        self,
        conn: sa.Connection,
        grant_id: str,
        approver_id: str,
        now: datetime,
        validity: timedelta,
    ) -> None:
        now_ts = int(now.timestamp())
        try:
            result = conn.execute(
                sa.text(
                    "UPDATE policy_grants SET status = 'approved', granted_by = :by, "
                    "granted_at_ts = :now, valid_to_ts = :valid_to "
                    "WHERE id = :id AND status = 'requested'"
                ),
                {
                    "id": grant_id,
                    "by": approver_id,
                    "now": now_ts,
                    "valid_to": now_ts + int(validity.total_seconds()),
                },
            )
        except IntegrityError as exc:
            if _SELF_APPROVAL_CHECK in str(exc.orig):
                raise AuthorizationDenied(  # noqa: TRY003
                    f"grant {grant_id!r}: the approver cannot be its requester",
                    code="self_approval",
                ) from exc
            raise
        if result.rowcount != 1:
            raise ConcurrencyConflict(f"grant {grant_id!r} was approved concurrently")  # noqa: TRY003
        self._emit(
            actor=approver_id,
            payload={"v": 1, "transition": "grant_approved", "grant_id": grant_id},
            ts=now,
        )

    def _has_admin(self, conn: sa.Connection) -> bool:
        row = conn.execute(
            sa.text(
                "SELECT 1 AS one FROM role_assignments WHERE tenant_id = :tenant AND role = 'admin'"
            ),
            {"tenant": self._tenant},
        ).first()
        return row is not None

    def _require_admin(self, conn: sa.Connection, user_id: str) -> None:
        user = self._load_user(conn, user_id)
        if user is None or not user.has_role(Role.ADMIN):
            raise AuthorizationDenied(  # noqa: TRY003
                f"user {user_id!r} is not an admin", code="rbac"
            )

    def _load_user(self, conn: sa.Connection, user_id: str) -> SessionUser | None:
        row = (
            conn.execute(
                sa.text(
                    "SELECT id, tenant_id, email, name FROM users "
                    "WHERE id = :id AND tenant_id = :tenant AND is_active = 1"
                ),
                {"id": user_id, "tenant": self._tenant},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        grants = frozenset(
            RoleGrant(Role(role), str(scope))
            for role, scope in conn.execute(
                sa.text(
                    "SELECT role, scope FROM role_assignments "
                    "WHERE user_id = :id AND tenant_id = :tenant"
                ),
                {"id": user_id, "tenant": self._tenant},
            )
        )
        return SessionUser(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            email=str(row["email"]),
            name=str(row["name"]),
            roles=frozenset(g.role for g in grants),
            grants=grants,
        )

    def _default_grant(
        self, conn: sa.Connection, kind: ActionKind, scope: str, now_ts: int
    ) -> DefaultGrant:
        row = (
            conn.execute(
                sa.text(
                    "SELECT id, status, valid_to_ts FROM policy_grants WHERE tenant_id = :tenant "
                    "AND action_kind = :kind AND scope = :scope "
                    "ORDER BY requested_at_ts DESC, id DESC"
                ),
                {"tenant": self._tenant, "kind": kind.value, "scope": scope},
            )
            .mappings()
            .first()
        )
        if row is None:
            return DefaultGrant(kind=kind, scope=scope, status="missing", grant_id=None)
        status: GrantStatus
        if row["status"] == "approved" and (row["valid_to_ts"] or 0) <= now_ts:
            status = "expired"
        else:
            status = row["status"]
        return DefaultGrant(kind=kind, scope=scope, status=status, grant_id=str(row["id"]))

    def _emit(self, *, actor: str, payload: dict[str, object], ts: datetime) -> None:
        """One ``gate`` event per authorization change (INV-03)."""
        self._events.append(
            Event(
                id=self._ids.new(IdPrefix.EVENT),
                tenant_id=self._tenant,
                trace_id=self._ids.new(IdPrefix.TRACE),
                span_id="span_setup",
                parent_span_id=None,
                kind=EventKind.GATE,
                actor=actor,
                payload=payload,
                ts=ts,
            )
        )


__all__ = [
    "DEFAULT_GRANT_VALIDITY",
    "DefaultGrant",
    "SessionUser",
    "SetupService",
    "SetupState",
    "expand_grant_requests",
]
