"""Policy grant: literal-scope standing authorization (ARCH §6.2, INV-01)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel


class GrantStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class Grant(DomainModel):
    """A policy_grants row; fields mirror the DDL (ARCH §6.2)."""

    id: str
    tenant_id: str
    action_kind: ActionKind
    scope: str  # literal scope string (e.g. "ticket:INC0000001")
    status: GrantStatus = GrantStatus.REQUESTED
    requested_by: str  # user id or "config:<sha>"
    requested_at: datetime
    granted_by: str | None = None
    granted_at: datetime | None = None
    valid_to: datetime | None = None
