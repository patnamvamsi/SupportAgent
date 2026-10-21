"""Outbox executor (WP-006, ARCH §6.2, INV-04): at-least-once delivery,
effectively-once effect.

``OutboxExecutor`` is the *application* service behind the outbox. ``enqueue``
inserts one outbox row under a valid authorization (the INV-01 trigger enforces
validity at the DB edge; a rejected insert surfaces as ``AuthorizationDenied``).
``run_once`` claims the next due row (CAS, exclusive — one worker wins), re-hashes
the payload (INV-01), reconciles the external state (INV-04), executes the action
through the ``TicketSink`` port, and records the outcome (done / retry / failed,
with a safe-default case takeover on final failure).

Transactions: the claim commits *before* the external call, and the outcome is
a second transaction fenced on the claim (``claimed_at_ts``). No database
transaction — on SQLite, no ``BEGIN IMMEDIATE`` write lock — is held across a
ServiceNow round-trip. A worker that dies between claim and outcome leaves the
row ``claimed``; after ``lease_s`` another worker reclaims it (one attempt
consumed) and reconciles before re-sending (INV-04).

Deliberately free of concrete persistence types (importlinter ``app-no-adapters``
/ ``only-bootstrap-wires``): the executor drives the unit of work through a
structural ``_UoW`` protocol and an injected ``_OutboxRepo``; bootstrap (or a
test) supplies the concrete ``SqlUnitOfWork`` + ``OutboxRepo``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.app.handlers import JOURNAL_KINDS, Handler, marker
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
    NotFound,
)
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import canonical_json, payload_hash
from supportagent.domain.idempotency import approval_key, grant_key
from supportagent.domain.ids import IdPrefix
from supportagent.domain.kinds import ActionKind
from supportagent.domain.lifecycle import CaseEvent, CaseStatus, next_status
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.event import Event
from supportagent.domain.model.outbox import OutboxRow
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.tickets import SnTable, TicketSink

#: The retry budget per outbox row (ARCH §6.2): after this many attempts the row
#: is ``failed`` and the case is safe-defaulted to ``human_owned``.
MAX_ATTEMPTS = 5

#: A claim older than this is presumed dead and may be reclaimed (01 §3
#: ``claim_next(now, lease_s)``). It must exceed the sink's worst-case time for
#: one claim (reconcile + send); production derives it from the sink's retry
#: budget (``bootstrap.outbox_claim_budget_s``, BL-001) and passes
#: ``send_budget_s`` so a too-short lease fails at startup.
DEFAULT_LEASE_S = 60

#: Exponential backoff (01 §7.5): 0.5 * 2**attempts seconds.
_BACKOFF_BASE_S = 0.5


def backoff_s(attempts: int) -> int:
    """Seconds to wait before attempt ``attempts`` (1-based)."""
    return int(_BACKOFF_BASE_S * (2**attempts))


class _UoW(Protocol):
    """The slice of the SQL unit of work the executor drives (01 §3).

    ``app`` may not import ``supportagent.adapters`` (importlinter), so the
    concrete ``SqlUnitOfWork`` is duck-typed: an active context manager exposing
    the raw connection and an explicit ``commit``.
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


class _OutboxRepo(Protocol):
    """The outbox write surface the executor needs (thin, trigger-safe)."""

    def get(self, outbox_id: str) -> dict[str, Any] | None: ...

    def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None: ...

    def insert(  # noqa: PLR0913
        self,
        *,
        id: str,
        tenant_id: str,
        case_id: str,
        action_kind: str,
        scope: str,
        payload_json: str,
        payload_hash: str,
        idempotency_key: str,
        approval_id: str | None,
        policy_grant_id: str | None,
        comms_plan_id: str | None,
        trace_id: str,
    ) -> None: ...

    def claim_next(self, *, now_ts: int, lease_s: int) -> dict[str, Any] | None: ...

    def mark_done(
        self, outbox_id: str, *, claimed_at_ts: int, external_ref: str | None, now_ts: int
    ) -> bool: ...

    def mark_failed(
        self,
        outbox_id: str,
        *,
        claimed_at_ts: int,
        error: str,
        retry_at_ts: int | None,
        consume_attempt: bool = True,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class Authorization:
    """One valid authorization path (INV-01): exactly one non-None id."""

    approval_id: str | None = None
    policy_grant_id: str | None = None
    comms_plan_id: str | None = None
    #: The comms path's key (01 §7.4) needs audience/trigger/slot, so the
    #: comms service derives it and passes it here (WP-102).
    plan_key: str | None = None

    def __post_init__(self) -> None:
        paths = (self.approval_id, self.policy_grant_id, self.comms_plan_id)
        if sum(p is not None for p in paths) != 1:
            raise DomainValidationError(  # noqa: TRY003
                "exactly one of approval_id / policy_grant_id / comms_plan_id must be set"
            )

    def idempotency_key(self, *, spec: ActionSpec, case_id: str, payload_hash: str) -> str:
        """Derive the UNIQUE idempotency key (01 §7.4) for this authorization."""
        if self.approval_id is not None:
            return approval_key(self.approval_id)
        if self.policy_grant_id is not None:
            return grant_key(
                case_id=case_id,
                action_kind=spec.kind.value,
                scope=spec.scope,
                payload_hash=payload_hash,
            )
        # comms-plan keys need audience/trigger/interval_slot (01 §7.4): the
        # comms service derives them. Without one, refuse rather than mint a
        # wrong key (INV-04).
        if self.plan_key is None:
            raise DomainValidationError(  # noqa: TRY003
                "comms-plan authorization requires the comms service to derive its key"
            )
        return self.plan_key


@dataclass(frozen=True, slots=True)
class _Claim:
    """A committed claim, validated and ready for the external call."""

    row: dict[str, Any]
    kind: ActionKind
    handler: Handler
    payload: Mapping[str, Any]
    table: SnTable
    sys_id: str
    token: int  # claimed_at_ts — the fencing token for the outcome write

    @property
    def outbox_id(self) -> str:
        return str(self.row["id"])

    @property
    def attempts(self) -> int:
        return int(self.row["attempts"])


@dataclass(frozen=True, slots=True)
class ExecResult:
    """The outcome of one ``run_once`` step (10-wp WP-006).

    ``status == "none"`` means no due row was claimed (the worker loop stops);
    ``"lost"`` means this worker's claim expired and was taken over before it
    could record the outcome (the new claimant reconciles).
    """

    outbox_id: str | None = None
    status: Literal["none", "sent", "reconciled", "retried", "failed", "lost"] = "none"
    attempts: int = 0
    external_ref: str | None = None
    error: str | None = None


class OutboxWriter:
    """Enqueue outbox rows (INV-01 edge enforced by the DB triggers, 03 §2.4).

    Split from ``OutboxExecutor`` so a path that only authorizes work — the
    approval API (WP-013) — enqueues without constructing a ``TicketSink``.
    Its ``with`` blocks join an enclosing unit of work, so an approval and its
    outbox row commit together.
    """

    def __init__(self, *, uow: _UoW, repo: _OutboxRepo, ids: IdGenerator) -> None:
        self._uow = uow
        self._repo = repo
        self._ids = ids

    def enqueue(
        self,
        authorization: Authorization,
        spec: ActionSpec,
        case_id: str,
        trace: TraceContext,
    ) -> OutboxRow:
        """Insert one outbox row under ``authorization`` (INV-01 edge enforced by DB).

        Idempotent on the derived key: a second ``enqueue`` with the same
        authorization + spec returns the existing row instead of inserting.
        """
        ph = payload_hash(spec.payload)
        key = authorization.idempotency_key(spec=spec, case_id=case_id, payload_hash=ph)
        with self._uow:
            existing = self._repo.get_by_key(key)
        if existing is not None:
            return _to_row(existing)

        outbox_id = self._ids.new(IdPrefix.OUTBOX)
        with self._uow:
            self._repo.insert(
                id=outbox_id,
                tenant_id=trace.tenant_id,
                case_id=case_id,
                action_kind=spec.kind.value,
                scope=spec.scope,
                payload_json=canonical_json(spec.payload),
                payload_hash=ph,
                idempotency_key=key,
                approval_id=authorization.approval_id,
                policy_grant_id=authorization.policy_grant_id,
                comms_plan_id=authorization.comms_plan_id,
                trace_id=trace.trace_id,
            )
            self._uow.commit()

        with self._uow:
            row = self._repo.get(outbox_id)
        if row is None:  # pragma: no cover — just committed; defensive only
            raise NotFound(f"outbox row {outbox_id} vanished after insert")  # noqa: TRY003
        return _to_row(row)


class OutboxExecutor:
    """Enqueue and execute outbox rows under INV-01 + INV-04 (WP-006)."""

    def __init__(  # noqa: PLR0913 — one port per constructor param (10-wp thin wiring)
        self,
        *,
        uow: _UoW,
        repo: _OutboxRepo,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        sink: TicketSink,
        handlers: Mapping[ActionKind, Handler],
        max_attempts: int = MAX_ATTEMPTS,
        lease_s: int = DEFAULT_LEASE_S,
        crash_hook: Callable[[str], None] | None = None,
        send_budget_s: float | None = None,
    ) -> None:
        if send_budget_s is not None and lease_s <= send_budget_s:
            raise ConfigError(  # noqa: TRY003
                f"outbox claim lease {lease_s}s must exceed the sink's worst-case "
                f"reconcile + send time {send_budget_s:.0f}s, or a live send can be "
                "reclaimed and sent twice (BL-001)"
            )
        self._uow = uow
        self._repo = repo
        self._clock = clock
        self._ids = ids
        self._events = events
        self._sink = sink
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._lease_s = lease_s
        self._crash_hook = crash_hook
        self._writer = OutboxWriter(uow=uow, repo=repo, ids=ids)

    # --- enqueue ------------------------------------------------------------

    def enqueue(
        self,
        authorization: Authorization,
        spec: ActionSpec,
        case_id: str,
        trace: TraceContext,
    ) -> OutboxRow:
        """Insert one outbox row under ``authorization`` (see ``OutboxWriter.enqueue``)."""
        return self._writer.enqueue(authorization, spec, case_id, trace)

    # --- run_once -----------------------------------------------------------

    def run_once(self, now: datetime) -> ExecResult:
        """Claim and process the next due outbox row (or report none due).

        Three steps, never one transaction across the external call:
        claim + validate (commit) → reconcile / send (no transaction) →
        record the outcome (commit, fenced on the claim).
        """
        now_ts = int(now.timestamp())
        claim = self._claim(now, now_ts)
        if isinstance(claim, ExecResult):
            return claim

        # INV-04: reconcile journal kinds against the external marker.
        key = str(claim.row["idempotency_key"])
        if claim.kind in JOURNAL_KINDS:
            try:
                found = self._sink.find_marker(claim.table, claim.sys_id, marker(key))
            except ExternalServiceError as exc:
                # Nothing was sent: an outage during the reconciliation read is
                # retried like a failed send (11-wp WP-106, INV-05).
                attempts = claim.attempts + 1
                if exc.retryable and attempts < self._max_attempts:
                    return self._record_retry(claim, now, now_ts, attempts, f"reconcile: {exc}")
                return self._finalize_failure(claim, now, attempts, f"reconcile failed: {exc}")
            if found is not None:
                return self._record_done(claim, now, now_ts, found.as_text(), "reconciled")
        self._crash("after_reconcile")

        # Stale-claim reclaims (a worker died mid-send) spend the same budget.
        if claim.attempts >= self._max_attempts:
            return self._finalize_failure(
                claim, now, claim.attempts, "retry budget exhausted", consume_attempt=False
            )

        return self._send(claim, now, now_ts, key)

    # --- steps --------------------------------------------------------------

    def _send(self, claim: _Claim, now: datetime, now_ts: int, key: str) -> ExecResult:
        """Execute the handler (no transaction open) and record the outcome."""
        try:
            ref = claim.handler.execute(
                sink=self._sink,
                table=claim.table,
                sys_id=claim.sys_id,
                payload=claim.payload,
                idempotency_key=key,
            )
        except DomainValidationError as exc:
            return self._finalize_failure(claim, now, claim.attempts + 1, f"invalid payload: {exc}")
        except ExternalServiceError as exc:
            attempts = claim.attempts + 1
            if exc.retryable and attempts < self._max_attempts:
                return self._record_retry(claim, now, now_ts, attempts, str(exc))
            return self._finalize_failure(claim, now, attempts, f"external failure: {exc}")

        # Sent. A crash from here to the commit is INV-04's replay case: the
        # row stays claimed, is reclaimed after lease_s, and reconciles.
        self._crash("after_send")
        return self._record_done(claim, now, now_ts, ref.as_text(), "sent")

    def _claim(self, now: datetime, now_ts: int) -> _Claim | ExecResult:
        """Claim, emit, re-hash and resolve the target; commit the claim."""
        with self._uow:
            row = self._repo.claim_next(now_ts=now_ts, lease_s=self._lease_s)
            if row is None:
                return ExecResult()
            self._crash("after_claim")
            self._emit(row, EventKind.SEND, now, {"phase": "claim"})
            token = int(row["claimed_at_ts"])

            # INV-01: the payload must still hash to what was authorized.
            payload: Mapping[str, Any] | None
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, ValueError):
                payload = None
            if not isinstance(payload, Mapping) or payload_hash(payload) != row["payload_hash"]:
                self._emit(
                    row,
                    EventKind.EXECUTOR_HASH_MISMATCH,
                    now,
                    {"phase": "abort", "error": "payload hash mismatch"},
                    status="error",
                )
                return self._abort(row, token, now, "payload hash mismatch (INV-01)")
            self._crash("after_rehash")

            kind = ActionKind(row["action_kind"])
            handler = self._handlers.get(kind)
            if handler is None:
                return self._abort(row, token, now, f"no handler for kind {kind.value}")

            case = self._case(row["case_id"])
            if case is None or not case.get("external_table") or not case.get("external_id"):
                return self._abort(row, token, now, "case has no external target")
            try:
                table = SnTable(case["external_table"])
            except ValueError:
                return self._abort(
                    row, token, now, f"unknown external table {case['external_table']!r}"
                )
            self._uow.commit()
        return _Claim(
            row=row,
            kind=kind,
            handler=handler,
            payload=payload,
            table=table,
            sys_id=str(case["external_id"]),
            token=token,
        )

    def _abort(self, row: dict[str, Any], token: int, now: datetime, error: str) -> ExecResult:
        """Fail a claimed row before any external call (inside the claim UoW)."""
        self._repo.mark_failed(row["id"], claimed_at_ts=token, error=error, retry_at_ts=None)
        self._emit(row, EventKind.SEND, now, {"phase": "abort", "error": error}, status="error")
        self._uow.commit()
        return ExecResult(
            outbox_id=row["id"],
            status="failed",
            attempts=int(row["attempts"]) + 1,
            error=error,
        )

    def _record_done(
        self,
        claim: _Claim,
        now: datetime,
        now_ts: int,
        external_ref: str,
        status: Literal["sent", "reconciled"],
    ) -> ExecResult:
        with self._uow:
            if not self._repo.mark_done(
                claim.outbox_id, claimed_at_ts=claim.token, external_ref=external_ref, now_ts=now_ts
            ):
                return self._lost(claim, now)
            self._crash("after_mark_done")
            self._mark_comms(claim.outbox_id, "sent", now_ts)
            self._emit(
                claim.row, EventKind.SEND, now, {"phase": status, "external_ref": external_ref}
            )
            self._uow.commit()
        return ExecResult(
            outbox_id=claim.outbox_id,
            status=status,
            attempts=claim.attempts,
            external_ref=external_ref,
        )

    def _record_retry(
        self, claim: _Claim, now: datetime, now_ts: int, attempts: int, error: str
    ) -> ExecResult:
        retry_at = now_ts + backoff_s(attempts)
        with self._uow:
            if not self._repo.mark_failed(
                claim.outbox_id, claimed_at_ts=claim.token, error=error, retry_at_ts=retry_at
            ):
                return self._lost(claim, now)
            self._emit(
                claim.row,
                EventKind.SEND,
                now,
                {"phase": "retry", "attempts": attempts, "retry_at_ts": retry_at},
                status="error",
            )
            self._uow.commit()
        return ExecResult(
            outbox_id=claim.outbox_id, status="retried", attempts=attempts, error=error
        )

    def _finalize_failure(
        self,
        claim: _Claim,
        now: datetime,
        attempts: int,
        error: str,
        *,
        consume_attempt: bool = True,
    ) -> ExecResult:
        """Mark failed (no retry), safe-default the case, emit, commit."""
        with self._uow:
            if not self._repo.mark_failed(
                claim.outbox_id,
                claimed_at_ts=claim.token,
                error=error,
                retry_at_ts=None,
                consume_attempt=consume_attempt,
            ):
                return self._lost(claim, now)
            self._safe_default(claim.row["case_id"])
            self._mark_comms(claim.outbox_id, "failed", None)
            self._emit(
                claim.row, EventKind.SEND, now, {"phase": "failed", "error": error}, status="error"
            )
            self._uow.commit()
        return ExecResult(
            outbox_id=claim.outbox_id, status="failed", attempts=attempts, error=error
        )

    def _mark_comms(self, outbox_id: str, status: str, sent_at_ts: int | None) -> None:
        """BL-034: a customer update's ``comms`` row follows its outbox outcome (ARCH §6.3)."""
        self._uow.connection.execute(
            sa.text(
                "UPDATE comms SET status = :s, sent_at_ts = :ts "
                "WHERE outbox_id = :id AND status = 'queued'"
            ),
            {"s": status, "ts": sent_at_ts, "id": outbox_id},
        )

    def _lost(self, claim: _Claim, now: datetime) -> ExecResult:
        """Our claim expired and was taken over; the new claimant owns the outcome."""
        self._emit(claim.row, EventKind.SEND, now, {"phase": "claim_lost"}, status="skipped")
        self._uow.commit()
        return ExecResult(outbox_id=claim.outbox_id, status="lost", attempts=claim.attempts)

    # --- internals ----------------------------------------------------------

    def _safe_default(self, case_id: str) -> None:
        """On final failure, move an active case to ``human_owned`` (ARCH §6.2)."""
        case = self._case(case_id)
        if case is None:
            return
        cur = case.get("status")
        if cur is None:
            return
        try:
            current = CaseStatus(cur)
        except ValueError:
            return
        try:
            new = next_status(current, CaseEvent.HUMAN_TAKEOVER)
        except DomainValidationError:
            return  # already human_owned / resolved: nothing to do
        if new is not CaseStatus.HUMAN_OWNED:
            return
        self._uow.connection.execute(
            sa.text("UPDATE cases SET status = :new WHERE id = :id AND status = :cur"),
            {"new": new.value, "id": case_id, "cur": cur},
        )

    def _case(self, case_id: str) -> dict[str, Any] | None:
        row = (
            self._uow.connection.execute(
                sa.text("SELECT status, external_table, external_id FROM cases WHERE id = :id"),
                {"id": case_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def _crash(self, step: str) -> None:
        """Test seam: simulate a crash at a numbered step (INV-04 crash points)."""
        if self._crash_hook is not None:
            self._crash_hook(step)

    def _emit(
        self,
        row: dict[str, Any],
        kind: EventKind,
        now: datetime,
        extra: Mapping[str, Any] | None = None,
        *,
        status: Literal["ok", "error", "skipped"] = "ok",
    ) -> None:
        payload: dict[str, Any] = {
            "v": 1,
            "outbox_id": row["id"],
            "action_kind": row["action_kind"],
        }
        if extra:
            payload.update(extra)
        self._events.append(
            Event(
                id=self._ids.new(IdPrefix.EVENT),
                tenant_id=row["tenant_id"],
                trace_id=row["trace_id"],
                span_id=f"exec-{row['id']}",
                parent_span_id=None,
                kind=kind,
                actor="executor",
                status=status,
                payload=payload,
                ts=now,
            )
        )


def _to_row(row: dict[str, Any]) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        case_id=row["case_id"],
        action_kind=ActionKind(row["action_kind"]),
        scope=row["scope"],
        payload_json=row["payload_json"],
        payload_hash=row["payload_hash"],
        idempotency_key=row["idempotency_key"],
        trace_id=row["trace_id"],
        approval_id=row.get("approval_id"),
        policy_grant_id=row.get("policy_grant_id"),
        comms_plan_id=row.get("comms_plan_id"),
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_ts=row.get("next_attempt_ts"),
        last_error=row.get("last_error"),
        external_ref=row.get("external_ref"),
        claimed_at_ts=row.get("claimed_at_ts"),
        done_at_ts=row.get("done_at_ts"),
    )


__all__ = [
    "DEFAULT_LEASE_S",
    "MAX_ATTEMPTS",
    "Authorization",
    "ExecResult",
    "OutboxExecutor",
    "OutboxWriter",
    "backoff_s",
]
