"""GateService (ARCH §6.2, 01 §7.10, INV-01): the human-authorization gate.

``GateService`` is the *application* service that mints and advances
proposals: it decides a proposal's initial status (auto-approved when a valid
policy grant covers the action, otherwise pending), records an approval when a
human signs off, and sweeps proposals that outlived their TTL. It is
deliberately free of any concrete persistence type (01 §1 layout,
importlinter): it drives the unit of work through a structural protocol and
speaks to the rest of the world through the ``ports`` and pure ``domain``.

INV-01 is enforced at the *edge* — the ``outbox`` write point and the
``approvals``/``policy_grants`` CHECKs (migration 0003) — not here. This
service only ever produces the *valid* state; the database is what makes the
invalid ones impossible to persist.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.app.outbox import Authorization
from supportagent.domain.errors import (
    AuthorizationDenied,
    ConcurrencyConflict,
    DomainValidationError,
    NotFound,
)
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import canonical_json, payload_hash
from supportagent.domain.ids import IdPrefix
from supportagent.domain.kinds import (
    CUSTOMER_FACING_KINDS,
    NEVER_EXECUTABLE_KINDS,
    ActionKind,
)
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.event import Event
from supportagent.domain.model.outbox import OutboxRow
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.proposal_state import (
    ProposalEvent,
    ProposalStatus,
    next_status,
)
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator


class _UoW(Protocol):
    """The slice of the SQL unit of work ``GateService`` drives (01 §3).

    ``app`` may not import ``supportagent.adapters`` (importlinter), so the
    concrete ``SqlUnitOfWork`` is duck-typed through this structural
    protocol: an active context manager exposing the raw connection and an
    explicit ``commit`` (commit is explicit, never implicit — 10-wp).
    """

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


class _Enqueuer(Protocol):
    """``OutboxWriter.enqueue``: joins the gate's unit of work (BL-024)."""

    def enqueue(
        self,
        authorization: Authorization,
        spec: ActionSpec,
        case_id: str,
        trace: TraceContext,
    ) -> OutboxRow: ...


class GateService:
    """Mint and advance proposals under the INV-01 authorization model (10-wp)."""

    # 6 injected ports/config, all keyword-only: uow + the 4 standard ports
    # (clock, ids, events) + domain config (catalog, ttl). PLR0913 suppressed:
    # the signature is the service's public contract, not a packing mistake.
    def __init__(  # noqa: PLR0913
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        catalog: Catalog,
        ttl: timedelta,
        outbox: _Enqueuer | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._catalog = catalog
        self._ttl = ttl
        self._outbox = outbox

    # --- submit -------------------------------------------------------------

    def submit(self, case_id: str, spec: ActionSpec, trace: TraceContext) -> Proposal:
        """Mint a proposal for ``spec`` and return it in its initial status.

        An unknown service target is a ``DomainValidationError`` raised before
        anything is written (INV-01: an unresolvable target is dropped). A
        valid policy grant auto-approves internal, executable kinds (no
        approval row); everything else starts ``pending_approval`` (01 §7.10).
        """
        known = {service.name for service in self._catalog.services}
        if spec.target.service not in known:
            raise DomainValidationError(  # noqa: TRY003 — raised before any DB write
                f"unknown service target '{spec.target.service}' for action "
                f"kind '{spec.kind.value}'"
            )

        kind = spec.kind
        now = self._clock.now()
        now_ts = int(now.timestamp())
        expires_ts = int((now + self._ttl).timestamp())
        hash_ = payload_hash(spec.payload)
        scope = spec.scope
        tenant_id = trace.tenant_id

        grant_eligible = kind not in NEVER_EXECUTABLE_KINDS and kind not in CUSTOMER_FACING_KINDS
        grant_id = self._covering_grant(tenant_id, kind, scope, now_ts) if grant_eligible else None
        if grant_id is not None:
            status = next_status(ProposalStatus.PROPOSED, ProposalEvent.GRANT_MATCHED)
        else:
            status = next_status(ProposalStatus.PROPOSED, ProposalEvent.SUBMITTED)

        proposal_id = self._ids.new(IdPrefix.PROPERTY)
        with self._uow:
            self._uow.connection.execute(
                sa.text(
                    "INSERT INTO proposals "
                    "(id, tenant_id, case_id, action_kind, scope, payload_json, "
                    "payload_hash, trace_id, status, created_by, created_at_ts, "
                    "expires_ts) "
                    "VALUES (:id, :tenant_id, :case_id, :action_kind, :scope, "
                    ":payload_json, :payload_hash, :trace_id, :status, 'agent', "
                    ":created_at_ts, :expires_ts)"
                ),
                {
                    "id": proposal_id,
                    "tenant_id": tenant_id,
                    "case_id": case_id,
                    "action_kind": kind.value,
                    "scope": scope,
                    "payload_json": canonical_json(spec.payload),
                    "payload_hash": hash_,
                    "trace_id": trace.trace_id,
                    "status": status.value,
                    "created_at_ts": now_ts,
                    "expires_ts": expires_ts,
                },
            )
            self._append_gate_event(
                tenant_id=tenant_id,
                trace_id=trace.trace_id,
                span_id=trace.span_id,
                parent_span_id=trace.parent_span_id,
                proposal_id=proposal_id,
                actor="agent",
                transition="submit",
                ts=now,
            )
            if grant_id is not None and self._outbox is not None:
                # BL-024: a grant-authorized proposal is executable now. Its
                # outbox row commits with it, like an approval's (ARCH §6.2).
                self._outbox.enqueue(Authorization(policy_grant_id=grant_id), spec, case_id, trace)
                status = next_status(status, ProposalEvent.OUTBOX_INSERTED)
                self._uow.connection.execute(
                    sa.text("UPDATE proposals SET status = :status WHERE id = :id"),
                    {"status": status.value, "id": proposal_id},
                )
                self._append_gate_event(
                    tenant_id=tenant_id,
                    trace_id=trace.trace_id,
                    span_id=trace.span_id,
                    parent_span_id=trace.parent_span_id,
                    proposal_id=proposal_id,
                    actor="agent",
                    transition="grant_enqueued",
                    ts=now,
                )
            self._uow.commit()

        return Proposal(
            id=proposal_id,
            tenant_id=tenant_id,
            case_id=case_id,
            action_kind=kind,
            scope=scope,
            payload=spec.payload,
            payload_hash=hash_,
            trace_id=trace.trace_id,
            status=status,
            created_by="agent",
            created_at=now,
        )

    # --- approve ------------------------------------------------------------

    def approve(
        self,
        proposal_id: str,
        approver_id: str,
        *,
        seen_hash: str,
        reason: str | None = None,
    ) -> str:
        """Record a human approval of a ``pending_approval`` proposal (01 §7.10).

        The approval is only valid against the payload hash the approver
        actually saw (``seen_hash``); a stale hash is an
        ``AuthorizationDenied``. A proposal already approved or executed is a
        ``ConcurrencyConflict`` (one approval, ever). Raising before any write
        leaves the proposal untouched (the unit of work rolls back).
        """
        now = self._clock.now()
        now_ts = int(now.timestamp())
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT tenant_id, status, payload_hash, trace_id "
                        "FROM proposals WHERE id = :id"
                    ),
                    {"id": proposal_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound(f"proposal not found: {proposal_id!r}")  # noqa: TRY003
            tenant_id = str(row["tenant_id"])
            current = ProposalStatus(str(row["status"]))
            stored_hash = str(row["payload_hash"])
            row_trace_id = str(row["trace_id"])

            if current in (ProposalStatus.APPROVED, ProposalStatus.EXECUTED):
                raise ConcurrencyConflict(  # noqa: TRY003 — the second signer loses
                    f"proposal already {current.value}: {proposal_id!r}"
                )
            if current is not ProposalStatus.PENDING_APPROVAL:
                raise DomainValidationError(  # noqa: TRY003
                    f"proposal is {current.value}, not pending_approval"
                )
            if seen_hash != stored_hash:
                raise AuthorizationDenied(  # noqa: TRY003 — a stale hash cannot be signed
                    f"payload hash mismatch for proposal {proposal_id!r}",
                    code="stale_proposal",
                )

            approved = next_status(current, ProposalEvent.APPROVED)
            approval_id = self._ids.new(IdPrefix.APPROVAL)
            self._uow.connection.execute(
                sa.text("UPDATE proposals SET status = :status WHERE id = :id"),
                {"status": approved.value, "id": proposal_id},
            )
            self._insert_approval(
                sa.text(
                    "INSERT INTO approvals "
                    "(id, tenant_id, proposal_id, approver_id, status, payload_hash, "
                    "reason, approved_at_ts, trace_id) "
                    "VALUES (:id, :tenant_id, :proposal_id, :approver_id, :status, "
                    ":payload_hash, :reason, :approved_at_ts, :trace_id)"
                ),
                {
                    "id": approval_id,
                    "tenant_id": tenant_id,
                    "proposal_id": proposal_id,
                    "approver_id": approver_id,
                    "status": "approved",
                    "payload_hash": stored_hash,
                    "reason": reason,
                    "approved_at_ts": now_ts,
                    "trace_id": row_trace_id,
                },
            )
            self._append_gate_event(
                tenant_id=tenant_id,
                trace_id=row_trace_id,
                span_id="gate_approve",
                parent_span_id=None,
                proposal_id=proposal_id,
                actor=approver_id,
                transition="approved",
                ts=now,
            )
            self._uow.commit()
        return approval_id

    # --- reject -------------------------------------------------------------

    def reject(
        self,
        proposal_id: str,
        approver_id: str,
        *,
        seen_hash: str,
        reason: str | None = None,
    ) -> str:
        """Record a human rejection of a ``pending_approval`` proposal (01 §7.10).

        Mirrors ``approve``: the decision is only valid against the payload
        hash the approver actually saw (``seen_hash``); a stale hash is an
        ``AuthorizationDenied``. A proposal already decided (approved, rejected,
        or executed) is a ``ConcurrencyConflict`` (one decision, ever). Raising
        before any write leaves the proposal untouched (the unit of work rolls
        back).
        """
        now = self._clock.now()
        now_ts = int(now.timestamp())
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT tenant_id, status, payload_hash, trace_id "
                        "FROM proposals WHERE id = :id"
                    ),
                    {"id": proposal_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise NotFound(f"proposal not found: {proposal_id!r}")  # noqa: TRY003
            tenant_id = str(row["tenant_id"])
            current = ProposalStatus(str(row["status"]))
            stored_hash = str(row["payload_hash"])
            row_trace_id = str(row["trace_id"])

            if current in (
                ProposalStatus.APPROVED,
                ProposalStatus.REJECTED,
                ProposalStatus.EXECUTED,
            ):
                raise ConcurrencyConflict(  # noqa: TRY003 — a second decision loses
                    f"proposal already {current.value}: {proposal_id!r}"
                )
            if current is not ProposalStatus.PENDING_APPROVAL:
                raise DomainValidationError(  # noqa: TRY003
                    f"proposal is {current.value}, not pending_approval"
                )
            if seen_hash != stored_hash:
                raise AuthorizationDenied(  # noqa: TRY003 — a stale hash cannot be decided
                    f"payload hash mismatch for proposal {proposal_id!r}",
                    code="stale_proposal",
                )

            rejected = next_status(current, ProposalEvent.REJECTED)
            approval_id = self._ids.new(IdPrefix.APPROVAL)
            self._uow.connection.execute(
                sa.text("UPDATE proposals SET status = :status WHERE id = :id"),
                {"status": rejected.value, "id": proposal_id},
            )
            self._insert_approval(
                sa.text(
                    "INSERT INTO approvals "
                    "(id, tenant_id, proposal_id, approver_id, status, payload_hash, "
                    "reason, approved_at_ts, trace_id) "
                    "VALUES (:id, :tenant_id, :proposal_id, :approver_id, :status, "
                    ":payload_hash, :reason, :approved_at_ts, :trace_id)"
                ),
                {
                    "id": approval_id,
                    "tenant_id": tenant_id,
                    "proposal_id": proposal_id,
                    "approver_id": approver_id,
                    "status": "rejected",
                    "payload_hash": stored_hash,
                    "reason": reason,
                    "approved_at_ts": now_ts,
                    "trace_id": row_trace_id,
                },
            )
            self._append_gate_event(
                tenant_id=tenant_id,
                trace_id=row_trace_id,
                span_id="gate_reject",
                parent_span_id=None,
                proposal_id=proposal_id,
                actor=approver_id,
                transition="rejected",
                ts=now,
            )
            self._uow.commit()
        return approval_id

    # --- expire -------------------------------------------------------------

    def expire_due(self, *, now: datetime) -> int:
        """Sweep ``pending_approval`` proposals whose TTL has elapsed (01 §7.10).

        Returns the count transitioned to ``expired``, one gate event per
        transition, all committed together.
        """
        now_ts = int(now.timestamp())
        with self._uow:
            rows = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT id, tenant_id, trace_id FROM proposals "
                        "WHERE status = 'pending_approval' AND expires_ts IS NOT NULL "
                        "AND expires_ts < :now_ts"
                    ),
                    {"now_ts": now_ts},
                )
                .mappings()
                .all()
            )
            expired = next_status(ProposalStatus.PENDING_APPROVAL, ProposalEvent.TTL_ELAPSED)
            for row in rows:
                proposal_id = str(row["id"])
                self._uow.connection.execute(
                    sa.text("UPDATE proposals SET status = :status WHERE id = :id"),
                    {"status": expired.value, "id": proposal_id},
                )
                self._append_gate_event(
                    tenant_id=str(row["tenant_id"]),
                    trace_id=str(row["trace_id"]),
                    span_id="gate_expire",
                    parent_span_id=None,
                    proposal_id=proposal_id,
                    actor="system",
                    transition="expired",
                    ts=now,
                )
            self._uow.commit()
        return len(rows)

    # --- helpers ------------------------------------------------------------

    def _insert_approval(self, statement: sa.TextClause, params: dict[str, object]) -> None:
        """Insert an ``approvals`` row; ``trg_no_self_approval`` → ``AuthorizationDenied``.

        The trigger (03 §2.3) is the enforcement; SQLite raises it as an
        IntegrityError and Postgres as a raised exception, so match its message.
        """
        try:
            self._uow.connection.execute(statement, params)
        except sa.exc.DBAPIError as exc:
            if "self-approval" in str(exc.orig):
                raise AuthorizationDenied(  # noqa: TRY003
                    f"approver {params['approver_id']!r} created this proposal",
                    code="self_approval",
                ) from exc
            raise

    def _covering_grant(
        self, tenant_id: str, kind: ActionKind, scope: str, now_ts: int
    ) -> str | None:
        """The approved, in-scope, unexpired policy grant covering the
        (tenant, kind, scope), if any — the same predicate as trigger path 2."""
        with self._uow:
            row = self._uow.connection.execute(
                sa.text(
                    "SELECT id FROM policy_grants "
                    "WHERE tenant_id = :tenant_id AND action_kind = :kind "
                    "AND scope = :scope AND status = 'approved' "
                    "AND valid_to_ts IS NOT NULL AND valid_to_ts > :now_ts"
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind.value,
                    "scope": scope,
                    "now_ts": now_ts,
                },
            ).first()
        return None if row is None else str(row[0])

    # 8 keyword params == the Event's required fields (tenant/trace/span/
    # parent_span) + proposal_id + actor + transition + ts. Natural set, not a
    # packing mistake.
    def _append_gate_event(  # noqa: PLR0913
        self,
        *,
        tenant_id: str,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        proposal_id: str,
        actor: str,
        transition: str,
        ts: datetime,
    ) -> None:
        """Append one ``gate`` event describing a proposal transition (INV-03)."""
        self._events.append(
            Event(
                id=self._ids.new(IdPrefix.EVENT),
                tenant_id=tenant_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                kind=EventKind.GATE,
                actor=actor,
                payload={
                    "v": 1,
                    "proposal_id": proposal_id,
                    "transition": transition,
                },
                ts=ts,
            )
        )


__all__ = ["GateService"]
