"""OutboxRow: the domain view of one outbox row (01 §4, ARCH §6.2).

The executor returns ``OutboxRow`` from ``enqueue`` and reads it back after a
claim. It is a frozen, strict snapshot of the ``outbox`` table (03 §2.3); the
mutable bookkeeping fields (``status``, ``attempts``, ...) carry server
defaults so a freshly-minted row can be built without them.
"""

from __future__ import annotations

from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel


class OutboxRow(DomainModel):
    """One row of the outbox (03 §2.3), as a domain value object."""

    id: str
    tenant_id: str
    case_id: str
    action_kind: ActionKind
    scope: str
    payload_json: str
    payload_hash: str
    idempotency_key: str
    trace_id: str
    approval_id: str | None = None
    policy_grant_id: str | None = None
    comms_plan_id: str | None = None
    status: str = "pending"
    attempts: int = 0
    next_attempt_ts: int | None = None
    last_error: str | None = None
    external_ref: str | None = None
    claimed_at_ts: int | None = None
    done_at_ts: int | None = None


__all__ = ["OutboxRow"]
