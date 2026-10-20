"""RBAC roles (ARCH §6.4, 03 §2.1): ranked, so a higher role implies the lower ones."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    APPROVER = "approver"
    ADMIN = "admin"


_RANKS: dict[Role, int] = {Role.VIEWER: 1, Role.ANALYST: 2, Role.APPROVER: 3, Role.ADMIN: 4}


def rank(role: Role) -> int:
    """Ordinal rank; larger is more privileged (viewer 1 ... admin 4)."""
    return _RANKS[role]


def satisfies(held: frozenset[Role], required: Role) -> bool:
    """True when any held role ranks at or above ``required`` (02 §2 role column)."""
    return any(rank(role) >= rank(required) for role in held)
