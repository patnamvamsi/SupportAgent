"""Approval rows (10-wp): the human sign-off on a proposal.

``ApprovalRepo`` is a thin, typed row accessor over the ``approvals`` table
(03 §4). The self-approval rule (approver ≠ creator) is enforced by the
``trg_no_self_approval`` trigger (migration 0003), not here; the repo merely
surfaces the driver error it raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import approvals
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewApproval:
    """The column set for a new ``approvals`` row (03 §4)."""

    id: str
    tenant_id: str
    proposal_id: str
    approver_id: str
    status: str
    payload_hash: str
    approved_at_ts: int
    reason: str | None = None
    trace_id: str | None = None


class ApprovalRepo:
    """Typed access to the ``approvals`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, row: NewApproval) -> None:
        """Insert a new approval row. The ``trg_no_self_approval`` trigger may
        reject the insert (approver == creator); that surfaces as a driver
        error the caller translates (see ``adapters.db.errors``)."""
        self._uow.connection.execute(
            sa.insert(approvals).values(
                id=row.id,
                tenant_id=row.tenant_id,
                proposal_id=row.proposal_id,
                approver_id=row.approver_id,
                status=row.status,
                payload_hash=row.payload_hash,
                reason=row.reason,
                approved_at_ts=row.approved_at_ts,
                trace_id=row.trace_id,
            )
        )

    def get(self, approval_id: str) -> dict[str, Any] | None:
        """Load one approval row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(approvals).where(approvals.c.id == approval_id))
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None


__all__ = ["ApprovalRepo", "NewApproval"]
