"""Comms-plan rows (10-wp): the pre-approved customer-facing communication.

``CommsPlanRepo`` is a thin, typed row accessor over the ``comms_plans`` table
(03 §4). The INV-01 rule "customer communication is never by policy grant"
(customer-facing kinds must be backed by a comms plan, not a grant) is enforced
by the ``trg_outbox_inv01`` trigger (migration 0003); the repo only persists
and loads plan rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import comms_plans
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewCommsPlan:
    """The column set for a new ``comms_plans`` row (03 §4)."""

    id: str
    tenant_id: str
    case_id: str
    audience: str
    channel: str
    template_id: str
    allowed_fields_json: str
    plan_hash: str
    status: str
    proposal_id: str  # ARCH §6.3: the plan is a proposal (kind 'comms_plan')
    approval_id: str | None = None
    cadence_minutes: int | None = None


class CommsPlanRepo:
    """Typed access to the ``comms_plans`` table (10-wp: thin)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, row: NewCommsPlan) -> None:
        """Insert a new comms-plan row (the CHECK constraint validates the
        status literal, 03 §1)."""
        self._uow.connection.execute(
            sa.insert(comms_plans).values(
                id=row.id,
                tenant_id=row.tenant_id,
                case_id=row.case_id,
                audience=row.audience,
                channel=row.channel,
                template_id=row.template_id,
                allowed_fields_json=row.allowed_fields_json,
                cadence_minutes=row.cadence_minutes,
                plan_hash=row.plan_hash,
                proposal_id=row.proposal_id,
                approval_id=row.approval_id,
                status=row.status,
            )
        )

    def get(self, plan_id: str) -> dict[str, Any] | None:
        """Load one comms-plan row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(comms_plans).where(comms_plans.c.id == plan_id))
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def set_status(self, plan_id: str, status: str) -> None:
        """Transition a plan's status (e.g. ``revoked``); the CHECK constraint
        validates the literal, and the INV-01 trigger blocks a revoked plan from
        backing an outbox row (10-wp WP-005)."""
        self._uow.connection.execute(
            sa.update(comms_plans).where(comms_plans.c.id == plan_id).values(status=status)
        )


__all__ = ["CommsPlanRepo", "NewCommsPlan"]
