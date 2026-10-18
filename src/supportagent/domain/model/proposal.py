"""Proposal: a proposed action awaiting authorization (ARCH §6.2, 01 §7.10)."""

from __future__ import annotations

from datetime import datetime

from supportagent.domain.hashing import _reject_floats
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel
from supportagent.domain.proposal_state import ProposalStatus


class Proposal(DomainModel):
    """One proposed action; fields mirror the proposals DDL (ARCH §6.2)."""

    id: str
    tenant_id: str
    case_id: str
    action_kind: ActionKind
    scope: str
    payload: dict[str, object]  # JsonValue domain; floats rejected by domain rules
    payload_hash: str  # payload_hash(payload); compared at execution (INV-04)
    trace_id: str
    status: ProposalStatus = ProposalStatus.PROPOSED
    created_by: str  # "agent" or a user id
    created_at: datetime

    def _assert_domain_rules(self) -> None:
        # 01 §7.1: floats are forbidden in hashed payloads.
        _reject_floats(self.payload)
