"""ApprovalService (02 §2 approve semantics, 10-wp WP-013).

Approve, exactly as 02 §2 specifies, within one UoW: load the proposal (must be
``pending_approval``) and compare the hash the approver saw (409
``stale_proposal`` on mismatch); insert the approval (``trg_no_self_approval``
→ 403); move the proposal to ``approved``; enqueue the outbox row carrying the
``approval_id``; commit. The executor sends asynchronously.

``GateService`` and ``OutboxWriter`` open their own ``with`` blocks on the same
UoW; nested blocks join this one, so nothing commits until all of it succeeds.

RBAC (11-wp WP-103): the decider's role grants must give ``DECIDE_PROPOSAL``
over the proposal's case service. An approver scoped to ``service:payments``
can't decide a ``search`` proposal. The check runs here, so the API and the
UI forms share it.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.app.outbox import Authorization
from supportagent.domain.errors import AuthorizationDenied, DomainValidationError, NotFound
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.outbox import OutboxRow
from supportagent.domain.rbac import Permission, RoleGrant, can, service_role_scope
from supportagent.domain.trace import TraceContext


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _Gate(Protocol):
    def approve(
        self, proposal_id: str, approver_id: str, *, seen_hash: str, reason: str | None = None
    ) -> str: ...

    def reject(
        self, proposal_id: str, approver_id: str, *, seen_hash: str, reason: str | None = None
    ) -> str: ...


class _Writer(Protocol):
    def enqueue(
        self, authorization: Authorization, spec: ActionSpec, case_id: str, trace: TraceContext
    ) -> OutboxRow: ...


@dataclass(frozen=True, slots=True)
class Approved:
    approval_id: str
    #: ``None`` for a comms plan: approving it authorizes later sends, it sends nothing.
    outbox_id: str | None


class ApprovalService:
    """Human decisions on proposals; an approval enqueues its outbox row atomically."""

    def __init__(self, *, uow: _UoW, gate: _Gate, outbox: _Writer, tenant_id: str) -> None:
        self._uow = uow
        self._gate = gate
        self._outbox = outbox
        self._tenant = tenant_id

    def approve(
        self,
        proposal_id: str,
        approver_id: str,
        *,
        seen_hash: str,
        grants: Collection[RoleGrant],
        reason: str | None = None,
    ) -> Approved:
        with self._uow:
            spec, case_id, trace_id = self._spec_for(proposal_id)
            _require_scope(grants, spec.target.service)
            approval_id = self._gate.approve(
                proposal_id, approver_id, seen_hash=seen_hash, reason=reason
            )
            if spec.kind is ActionKind.COMMS_PLAN:
                # ARCH §6.3: the approval *is* the plan's authorization; each
                # cadence send is its own outbox row on the plan path (WP-102).
                self._set_plan(proposal_id, status="approved", approval_id=approval_id)
                self._uow.commit()
                return Approved(approval_id=approval_id, outbox_id=None)
            row = self._outbox.enqueue(
                Authorization(approval_id=approval_id),
                spec,
                case_id,
                TraceContext.new(self._tenant, trace_id, "span_outbox_enqueue"),
            )
            self._uow.commit()
        return Approved(approval_id=approval_id, outbox_id=row.id)

    def reject(
        self,
        proposal_id: str,
        approver_id: str,
        *,
        seen_hash: str,
        reason: str,
        grants: Collection[RoleGrant],
    ) -> str:
        """Reject with a required reason (02 §2: the reason is the feedback, ARCH §8.3)."""
        if not reason.strip():
            raise DomainValidationError("a rejection requires a reason")  # noqa: TRY003
        with self._uow:
            spec, _, _ = self._spec_for(proposal_id)  # tenant-scoped existence check
            _require_scope(grants, spec.target.service)
            approval_id = self._gate.reject(
                proposal_id, approver_id, seen_hash=seen_hash, reason=reason.strip()
            )
            if spec.kind is ActionKind.COMMS_PLAN:
                self._set_plan(proposal_id, status="rejected", approval_id=None)
            self._uow.commit()
        return approval_id

    def _set_plan(self, proposal_id: str, *, status: str, approval_id: str | None) -> None:
        self._uow.connection.execute(
            sa.text(
                "UPDATE comms_plans SET status = :status, "
                "approval_id = COALESCE(:approval, approval_id) "
                "WHERE proposal_id = :proposal AND tenant_id = :tenant "
                "AND status = 'pending_approval'"
            ),
            {
                "status": status,
                "approval": approval_id,
                "proposal": proposal_id,
                "tenant": self._tenant,
            },
        )

    def _spec_for(self, proposal_id: str) -> tuple[ActionSpec, str, str]:
        """The proposal's exact ``ActionSpec``: the approved bytes are the enqueued bytes."""
        row = (
            self._uow.connection.execute(
                sa.text(
                    "SELECT p.case_id, p.action_kind, p.scope, p.payload_json, p.trace_id, "
                    "c.service, c.external_id FROM proposals p JOIN cases c ON c.id = p.case_id "
                    "WHERE p.id = :id AND p.tenant_id = :tenant"
                ),
                {"id": proposal_id, "tenant": self._tenant},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound(f"proposal not found: {proposal_id!r}")  # noqa: TRY003
        spec = ActionSpec(
            kind=ActionKind(row["action_kind"]),
            target=TargetRef(
                service=str(row["service"] or ""),
                system="servicenow",
                ref=str(row["external_id"]),
            ),
            scope=str(row["scope"]),
            payload=json.loads(row["payload_json"]),
        )
        return spec, str(row["case_id"]), str(row["trace_id"])


def _require_scope(grants: Collection[RoleGrant], service: str) -> None:
    if not can(grants, Permission.DECIDE_PROPOSAL, service_role_scope(service or None)):
        raise AuthorizationDenied(  # noqa: TRY003
            f"your approver role doesn't cover service {service or '(unresolved)'!r}",
            code="rbac",
        )


__all__ = ["ApprovalService", "Approved"]
