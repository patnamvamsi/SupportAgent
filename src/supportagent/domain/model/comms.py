"""CommsPlan: an approved communication plan (01 §7.7, ARCH §6.3)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from supportagent.domain.model.base import DomainModel


class CommsPlanStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    SATISFIED = "satisfied"


class CommsPlan(DomainModel):
    """A comms_plans row; fields mirror the DDL (ARCH §6.3)."""

    id: str
    tenant_id: str
    case_id: str
    audience: Literal["customer", "internal"]
    channel: str
    template_id: str
    allowed_fields: dict[str, str]  # field -> source: "record_state" | "schedule"
    cadence_minutes: int | None
    plan_hash: str  # plan_hash(audience, channel, template, fields, cadence)
    proposal_id: str
    approval_id: str | None
    status: CommsPlanStatus = CommsPlanStatus.PENDING_APPROVAL
