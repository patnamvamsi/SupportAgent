"""Proposal rows (10-wp): the INV-01 gate's candidate action.

``ProposalRepo`` is a thin, typed row accessor over the ``proposals`` table
(03 §4). It takes the active ``SqlUnitOfWork`` and runs on its connection, so
it participates in the unit of work's transaction (commit-on-demand, roll-back
otherwise). It holds no orchestration (10-wp: thin repository).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import proposals
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewProposal:
    """The column set for a new ``proposals`` row (03 §4)."""

    id: str
    tenant_id: str
    case_id: str
    action_kind: str
    scope: str
    payload_json: str
    payload_hash: str
    trace_id: str
    status: str
    created_by: str
    created_at_ts: int
    expires_ts: int | None = None


class ProposalRepo:
    """Typed access to the ``proposals`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, row: NewProposal) -> None:
        """Insert a new proposal row (status is a caller-supplied literal; the
        CHECK constraint validates it, 03 §1)."""
        self._uow.connection.execute(
            sa.insert(proposals).values(
                id=row.id,
                tenant_id=row.tenant_id,
                case_id=row.case_id,
                action_kind=row.action_kind,
                scope=row.scope,
                payload_json=row.payload_json,
                payload_hash=row.payload_hash,
                trace_id=row.trace_id,
                status=row.status,
                created_by=row.created_by,
                created_at_ts=row.created_at_ts,
                expires_ts=row.expires_ts,
            )
        )

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """Load one proposal row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(proposals).where(proposals.c.id == proposal_id))
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def set_status(self, proposal_id: str, status: str) -> int:
        """CAS the proposal ``status``; returns the rowcount (0 == lost the
        optimistic-concurrency race). The caller raises ``ConcurrencyConflict``
        when the rowcount is 0 (10-wp WP-005 ``approve``)."""
        return self._uow.connection.execute(
            sa.update(proposals)
            .where(proposals.c.id == proposal_id, proposals.c.status == "pending_approval")
            .values(status=status)
        ).rowcount


__all__ = ["NewProposal", "ProposalRepo"]
