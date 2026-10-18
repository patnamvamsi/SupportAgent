"""Outbox executor (WP-006, ARCH §6.2, INV-04): at-least-once delivery,
effectively-once effect.

``OutboxExecutor`` is the *application* service behind the outbox. ``enqueue``
inserts one outbox row under a valid authorization (the INV-01 trigger enforces
validity at the DB edge; a rejected insert surfaces as ``AuthorizationDenied``).
``run_once`` claims the next due row (CAS, exclusive — one worker wins), re-hashes
the payload (INV-01), reconciles the external state (INV-04), executes the action
through the ``TicketSink`` port, and records the outcome (done / retry / failed,
with a safe-default case takeover on final failure).

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
from typing import Any, Literal, Protocol

import sqlalchemy as sa

from supportagent.app.handlers import JOURNAL_KINDS, Handler, marker
from supportagent.domain.errors import (
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

    connection: Any

    def commit(self) -> None: ...

    def __enter__(self) -> _UoW: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


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

    def claim_next(self, *, now_ts: int) -> dict[str, Any] | None: ...

    def mark_done(self, outbox_id: str, *, external_ref: str | None, now_ts: int) -> None: ...

    def mark_failed(self, outbox_id: str, *, error: str, retry_at_ts: int | None) -> None: ...


@dataclass(frozen=True, slots=True)
class Authorization:
    """One valid authorization path (INV-01): exactly one non-None id."""

    approval_id: str | None = None
    policy_grant_id: str | None = None
    comms_plan_id: str | None = None

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
        # comms-plan keys need audience/trigger/interval_slot, which the v0.0
        # enqueue signature does not carry (01 §7.4); the comms service derives
        # them. Refuse here rather than mint a wrong key (INV-04).
        raise DomainValidationError(  # noqa: TRY003
            "comms-plan authorization requires the comms service to derive its key"
        )


@dataclass(frozen=True, slots=True)
class ExecResult:
    """The outcome of one ``run_once`` step (10-wp WP-006).

    ``status == "none"`` means no due row was claimed (the worker loop stops).
    """

    outbox_id: str | None = None
    status: Literal["none", "sent", "reconciled", "retried", "failed"] = "none"
    attempts: int = 0
    external_ref: str | None = None
    error: str | None = None


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
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._uow = uow
        self._repo = repo
        self._clock = clock
        self._ids = ids
        self._events = events
        self._sink = sink
        self._handlers = handlers
        self._max_attempts = max_attempts
        self._crash_hook = crash_hook

    # --- enqueue ------------------------------------------------------------

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
            return self._to_row(existing)

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
        return self._to_row(row)

    # --- run_once -----------------------------------------------------------

    # PLR0911/PLR0915: a linear crash-recovery state machine — each branch
    # commits and returns immediately, with crash hooks interleaved (INV-04);
    # a single-exit restructure would obscure those recovery semantics.
    def run_once(self, now: datetime) -> ExecResult:  # noqa: PLR0911, PLR0915
        """Claim and process the next due outbox row (or report none due)."""
        now_ts = int(now.timestamp())
        with self._uow:
            row = self._repo.claim_next(now_ts=now_ts)
            if row is None:
                return ExecResult()
            outbox_id = row["id"]
            kind = ActionKind(row["action_kind"])
            attempts_before = int(row["attempts"])
            self._crash("after_claim")
            self._emit(row, EventKind.SEND, now, {"phase": "claim"})

            # INV-01: the payload must still hash to what was authorized.
            try:
                payload: Mapping[str, Any] = json.loads(row["payload_json"])
            except (json.JSONDecodeError, ValueError):
                payload = None  # type: ignore[assignment]
            if payload is None or payload_hash(payload) != row["payload_hash"]:
                self._repo.mark_failed(
                    outbox_id, error="payload hash mismatch (INV-01)", retry_at_ts=None
                )
                self._emit(
                    row,
                    EventKind.EXECUTOR_HASH_MISMATCH,
                    now,
                    {"phase": "abort", "error": "payload hash mismatch"},
                    status="error",
                )
                self._emit(row, EventKind.SEND, now, {"phase": "abort"}, status="error")
                self._uow.commit()
                return ExecResult(
                    outbox_id=outbox_id,
                    status="failed",
                    attempts=attempts_before,
                    error="payload hash mismatch (INV-01)",
                )
            self._crash("after_rehash")

            # Resolve the external target from the case.
            case = self._case(row["case_id"])
            if case is None or not case.get("external_table") or not case.get("external_id"):
                self._repo.mark_failed(
                    outbox_id, error="case has no external target", retry_at_ts=None
                )
                self._emit(
                    row,
                    EventKind.SEND,
                    now,
                    {"phase": "abort", "error": "no external target"},
                    status="error",
                )
                self._uow.commit()
                return ExecResult(
                    outbox_id=outbox_id,
                    status="failed",
                    attempts=attempts_before,
                    error="case has no external target",
                )

            try:
                table = SnTable(case["external_table"])
            except ValueError:
                self._repo.mark_failed(
                    outbox_id,
                    error=f"unknown external table {case['external_table']!r}",
                    retry_at_ts=None,
                )
                self._emit(
                    row,
                    EventKind.SEND,
                    now,
                    {"phase": "abort", "error": "unknown external table"},
                    status="error",
                )
                self._uow.commit()
                return ExecResult(
                    outbox_id=outbox_id,
                    status="failed",
                    attempts=attempts_before,
                    error="unknown external table",
                )
            sys_id = str(case["external_id"])
            key = row["idempotency_key"]

            # INV-04: reconcile journal kinds against the external marker.
            if kind in JOURNAL_KINDS:
                found = self._sink.find_marker(table, sys_id, marker(key))
                if found is not None:
                    self._repo.mark_done(outbox_id, external_ref=found.as_text(), now_ts=now_ts)
                    self._emit(
                        row,
                        EventKind.SEND,
                        now,
                        {"phase": "reconciled", "external_ref": found.as_text()},
                    )
                    self._uow.commit()
                    return ExecResult(
                        outbox_id=outbox_id,
                        status="reconciled",
                        attempts=attempts_before,
                        external_ref=found.as_text(),
                    )
            self._crash("after_reconcile")

            handler = self._handlers.get(kind)
            if handler is None:
                self._repo.mark_failed(
                    outbox_id, error=f"no handler for kind {kind.value}", retry_at_ts=None
                )
                self._emit(
                    row,
                    EventKind.SEND,
                    now,
                    {"phase": "abort", "error": "no handler"},
                    status="error",
                )
                self._uow.commit()
                return ExecResult(
                    outbox_id=outbox_id,
                    status="failed",
                    attempts=attempts_before,
                    error="no handler for kind",
                )

            try:
                ref = handler.execute(
                    sink=self._sink,
                    table=table,
                    sys_id=sys_id,
                    payload=payload,
                    idempotency_key=key,
                )
            except DomainValidationError as exc:
                return self._finalize_failure(
                    row, now, now_ts, attempts_before, f"invalid payload: {exc}"
                )
            except ExternalServiceError as exc:
                attempts = attempts_before + 1
                if exc.retryable and attempts < self._max_attempts:
                    retry_at = now_ts + backoff_s(attempts)
                    self._repo.mark_failed(outbox_id, error=str(exc), retry_at_ts=retry_at)
                    self._emit(
                        row,
                        EventKind.SEND,
                        now,
                        {"phase": "retry", "attempts": attempts, "retry_at_ts": retry_at},
                        status="error",
                    )
                    self._uow.commit()
                    return ExecResult(
                        outbox_id=outbox_id,
                        status="retried",
                        attempts=attempts,
                        error=str(exc),
                    )
                return self._finalize_failure(
                    row, now, now_ts, attempts, f"external failure: {exc}"
                )

            # Sent: record it, then commit (a crash here is INV-04's replay case).
            self._crash("after_send")
            self._repo.mark_done(outbox_id, external_ref=ref.as_text(), now_ts=now_ts)
            self._crash("after_mark_done")
            self._emit(
                row,
                EventKind.SEND,
                now,
                {"phase": "sent", "external_ref": ref.as_text()},
            )
            self._uow.commit()
            return ExecResult(
                outbox_id=outbox_id,
                status="sent",
                attempts=attempts_before,
                external_ref=ref.as_text(),
            )

    # --- internals ----------------------------------------------------------

    def _finalize_failure(
        self,
        row: dict[str, Any],
        now: datetime,
        now_ts: int,
        attempts: int,
        error: str,
    ) -> ExecResult:
        """Mark failed (no retry), safe-default the case, emit, commit."""
        outbox_id = row["id"]
        self._repo.mark_failed(outbox_id, error=error, retry_at_ts=None)
        self._safe_default(row["case_id"])
        self._emit(row, EventKind.SEND, now, {"phase": "failed", "error": error}, status="error")
        self._uow.commit()
        return ExecResult(outbox_id=outbox_id, status="failed", attempts=attempts, error=error)

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

    def _to_row(self, row: dict[str, Any]) -> OutboxRow:
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
    "MAX_ATTEMPTS",
    "Authorization",
    "ExecResult",
    "OutboxExecutor",
    "backoff_s",
]
