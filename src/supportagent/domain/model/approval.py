"""Approval: one person's decision on one proposal (ARCH §6.2, INV-01)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from supportagent.domain.model.base import DomainModel


class ApprovalStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Approval(DomainModel):
    """An approval record; fields mirror the approvals DDL (ARCH §6.2)."""

    id: str
    tenant_id: str
    proposal_id: str
    approver_id: str
    status: ApprovalStatus
    payload_hash: str  # the hash the approver actually saw (INV-04)
    reason: str | None
    approved_at: datetime
