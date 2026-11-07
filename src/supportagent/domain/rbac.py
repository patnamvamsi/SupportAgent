"""RBAC permissions (ARCH §6.4, 11-wp WP-103): the role x permission matrix.

| Permission | viewer | analyst | approver | admin |
|---|---|---|---|---|
| ``VIEW`` cases, proposals, stats | ✓ | ✓ | ✓ | ✓ |
| ``HAND_BACK`` a case | | ✓ | ✓ | ✓ |
| ``DECIDE_PROPOSAL`` approve / reject (scope-limited) | | | ✓ | ✓ |
| ``REVIEW_KNOWLEDGE`` confirm or reject the system graph (ADR-0015) | | ✓ | ✓ | ✓ |
| ``ADMINISTER`` grants, automation actors, roles | | | | ✓ |

A role assignment carries a scope: ``*`` (everything) or
``service:<name>`` (one catalog service, the same service granularity as
ADR-0005). ``can`` asks whether any held assignment both grants the
permission and covers the target's scope. A check without a target scope
(``scope=None``) asks only "does the user hold the permission anywhere",
which gates a page; the action itself is checked against its target.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.roles import Role

ALL_SCOPE = "*"
_SERVICE_SCOPE = re.compile(r"^service:[a-z0-9][a-z0-9-]{1,62}$")


class Permission(StrEnum):
    VIEW = "view"
    HAND_BACK = "hand_back"
    DECIDE_PROPOSAL = "decide_proposal"
    ADMINISTER = "administer"
    REVIEW_KNOWLEDGE = "review_knowledge"
    #: ADR-0021: trace a business object through a flow (reads production data).
    TRACE = "trace"


MATRIX: Mapping[Permission, frozenset[Role]] = {
    Permission.VIEW: frozenset(Role),
    Permission.HAND_BACK: frozenset({Role.ANALYST, Role.APPROVER, Role.ADMIN}),
    Permission.DECIDE_PROPOSAL: frozenset({Role.APPROVER, Role.ADMIN}),
    Permission.ADMINISTER: frozenset({Role.ADMIN}),
    Permission.REVIEW_KNOWLEDGE: frozenset({Role.ANALYST, Role.APPROVER, Role.ADMIN}),
    Permission.TRACE: frozenset({Role.ANALYST, Role.APPROVER, Role.ADMIN}),
}


@dataclass(frozen=True, slots=True)
class RoleGrant:
    """One ``role_assignments`` row: a role, limited to ``scope``."""

    role: Role
    scope: str = ALL_SCOPE


def validate_scope(role: Role, scope: str) -> str:
    """``*`` or ``service:<name>``; an admin is never scope-limited."""
    if scope != ALL_SCOPE and not _SERVICE_SCOPE.match(scope):
        raise DomainValidationError(  # noqa: TRY003
            f"role scope must be '*' or 'service:<name>'; got {scope!r}"
        )
    if role is Role.ADMIN and scope != ALL_SCOPE:
        raise DomainValidationError("the admin role is tenant-wide (scope '*')")  # noqa: TRY003
    return scope


def service_role_scope(service: str | None) -> str | None:
    """The scope a case of ``service`` belongs to (``None``: an unresolved service)."""
    return None if service is None else f"service:{service}"


def _covers(granted: str, target: str | None) -> bool:
    return granted == ALL_SCOPE or (target is not None and granted == target)


def can(
    grants: Iterable[RoleGrant],
    permission: Permission,
    scope: str | None = None,
    *,
    anywhere: bool = False,
) -> bool:
    """True when some grant gives ``permission`` over ``scope``.

    ``anywhere=True`` ignores the target (page access: "holds it in some
    scope"). A case with no resolved service (``scope=None``) is covered only
    by a tenant-wide (``*``) grant.
    """
    allowed = MATRIX[permission]
    return any(g.role in allowed and (anywhere or _covers(g.scope, scope)) for g in grants)


__all__ = [
    "ALL_SCOPE",
    "MATRIX",
    "Permission",
    "RoleGrant",
    "can",
    "service_role_scope",
    "validate_scope",
]
