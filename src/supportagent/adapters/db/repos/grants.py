"""Policy-grant rows (10-wp): the standing, pre-approved authorization.

``GrantRepo`` is a thin, typed row accessor over the ``policy_grants`` table
(03 §4). The INV-01 grant path (an outbox row backed by an approved, in-scope,
unexpired, non-customer-facing grant) is enforced by the ``trg_outbox_inv01``
trigger (migration 0003); the repo only exposes the lookup the gate needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import policy_grants
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewGrant:
    """The column set for a new ``policy_grants`` row (03 §4)."""

    id: str
    tenant_id: str
    action_kind: str
    scope: str
    status: str
    requested_by: str
    requested_at_ts: int
    granted_by: str | None = None
    granted_at_ts: int | None = None
    valid_to_ts: int | None = None


class GrantRepo:
    """Typed access to the ``policy_grants`` table (10-wp: thin)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, row: NewGrant) -> None:
        """Insert a new grant row (the self-approval CHECK constraint and the
        0003 trigger both guard against ``granted_by == requested_by``)."""
        self._uow.connection.execute(
            sa.insert(policy_grants).values(
                id=row.id,
                tenant_id=row.tenant_id,
                action_kind=row.action_kind,
                scope=row.scope,
                status=row.status,
                requested_by=row.requested_by,
                requested_at_ts=row.requested_at_ts,
                granted_by=row.granted_by,
                granted_at_ts=row.granted_at_ts,
                valid_to_ts=row.valid_to_ts,
            )
        )

    def get(self, grant_id: str) -> dict[str, Any] | None:
        """Load one grant row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(
                sa.select(policy_grants).where(policy_grants.c.id == grant_id)
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def find_approved_for(
        self,
        *,
        tenant_id: str,
        action_kind: str,
        scope: str,
        now_ts: int,
    ) -> dict[str, Any] | None:
        """The approved grant matching ``(tenant, kind, scope)`` exactly that is
        still valid at ``now_ts`` (``valid_to_ts > now_ts``), or ``None``. This
        is the lookup the gate's ``submit`` grant path uses (10-wp WP-005)."""
        row = (
            self._uow.connection.execute(
                sa.select(policy_grants)
                .where(
                    policy_grants.c.tenant_id == tenant_id,
                    policy_grants.c.action_kind == action_kind,
                    policy_grants.c.scope == scope,
                    policy_grants.c.status == "approved",
                    policy_grants.c.valid_to_ts.is_not(None),
                    policy_grants.c.valid_to_ts > now_ts,
                )
                .order_by(policy_grants.c.granted_at_ts.desc())
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None


__all__ = ["GrantRepo", "NewGrant"]
