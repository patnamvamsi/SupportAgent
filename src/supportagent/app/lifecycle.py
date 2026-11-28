"""LifecycleService (ARCH §8.1, §8.2; 01 §7.11; 11-wp WP-101).

``apply(case_id, changes)`` turns a known record's per-change history into
lifecycle transitions (``domain.takeover.lifecycle_signals``) and applies
them in one unit of work, each as a compare-and-swap on ``cases.status``:

- **takeover** → ``human_owned``: a ``sync_takeover`` event with the actor,
  field and ``sys_mod_count``; every ``pending_approval`` proposal of the case
  is ``superseded`` (a ``gate`` event each). New submits are then refused by
  the gate (``Skipped(reason="human_owned")``).
- **resolved** → ``resolved`` (``resolved_ts`` set; the fingerprint is free again).
- **reopened** → ``reopened``, unless a newer unresolved case already holds
  the fingerprint (INV-02). Then the case stays resolved and a ``sync`` event
  records the conflict.
- **hand-back by re-assignment** to the integration user → ``in_progress``.

``hand_back(case_id, actor_id)`` is the explicit UI/API hand-back (ARCH §8.2:
"the agent never silently resumes"): ``human_owned → in_progress`` with a
``gate`` event naming the user. A lost CAS (the status changed underneath)
is a ``ConcurrencyConflict``. The sync poll then retries the batch, because
nothing was marked seen.

``record_outcome(case_id, actor_id, ...)`` (ADR-0024) resolves a case that has no
ticket: a person records its outcome (label, severity, their verdict on the RCA's
cause, a drill or not) and the case moves to ``resolved``, with a ``gate`` event and
the ``outcome`` feedback in one unit of work. A ticket's case resolves only through
its ticket.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.connector import TicketSemantics
from supportagent.domain.errors import (
    AuthorizationDenied,
    ConcurrencyConflict,
    DomainValidationError,
    NotFound,
)
from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.lifecycle import CaseEvent, CaseStatus, next_status
from supportagent.domain.model.event import Event
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.domain.proposal_state import ProposalEvent, ProposalStatus
from supportagent.domain.proposal_state import next_status as next_proposal_status
from supportagent.domain.rbac import Permission, RoleGrant, can, service_role_scope
from supportagent.domain.severity import Severity
from supportagent.domain.takeover import LifecycleSignal, lifecycle_signals
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator


class _Feedback(Protocol):
    """``FeedbackService.record_case_outcome`` (ADR-0024)."""

    def record_case_outcome(  # noqa: PLR0913 — the outcome's fields
        self,
        case_id: str,
        *,
        resolved_by: str,
        classification: str,
        severity: Severity,
        cause: Literal["right", "wrong"] | None,
        drill: bool,
    ) -> None: ...


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


_SUPERSEDED = next_proposal_status(ProposalStatus.PENDING_APPROVAL, ProposalEvent.CASE_RETRIAGED)


class LifecycleService:
    def __init__(  # noqa: PLR0913 — the ports plus the actor-identity inputs
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        tenant_id: str,
        semantics: TicketSemantics,
        integration_user: str = "",
        integration_ids: Callable[[], Collection[str]] | None = None,
        feedback: _Feedback | None = None,
    ) -> None:
        self._uow = uow
        self._feedback = feedback
        self._clock = clock
        self._ids = ids
        self._events = events
        self._tenant = tenant_id
        self._integration_user = integration_user
        self._integration_ids = integration_ids or (lambda: frozenset({integration_user}))
        self._semantics = semantics

    # --- connector-driven ------------------------------------------------------

    def apply(self, case_id: str, changes: Sequence[ChangeRecord]) -> list[LifecycleSignal]:
        """Apply the lifecycle events in ``changes`` to the case; the applied signals."""
        with self._uow:
            case = self._case(case_id)
            signals = lifecycle_signals(
                changes,
                CaseStatus(str(case["status"])),
                integration_user=self._integration_user,
                integration_ids=frozenset(self._integration_ids()) | {self._integration_user},
                automation_actors=self._automation_actors(),
                semantics=self._semantics,
            )
            applied: list[LifecycleSignal] = []
            for signal in signals:
                if self._apply_one(case, signal):
                    applied.append(signal)
                else:
                    break  # a refused reopen: the case stays where it is
            self._uow.commit()
        return applied

    def _apply_one(self, case: dict[str, object], signal: LifecycleSignal) -> bool:
        case_id = str(case["id"])
        change = signal.change
        detail: dict[str, object] = {
            "v": 1,
            "transition": signal.event.value,
            "from": signal.before.value,
            "to": signal.after.value,
            "actor": change.user,
            "field": change.fieldname,
            "sys_mod_count": change.checkpoint,
        }
        if signal.event is CaseEvent.REOPENED and self._fingerprint_taken(case):
            self._emit(case, EventKind.SYNC, change.user, {**detail, "refused": "fingerprint_open"})
            return False
        now_ts = int(self._clock.now().timestamp())
        resolved_ts: int | None
        if signal.event is CaseEvent.RESOLVED:
            resolved_ts = now_ts
        elif signal.event is CaseEvent.REOPENED:
            resolved_ts = None
        else:
            resolved_ts = _keep(case)
        self._cas(case_id, signal.before, signal.after, resolved_ts)
        if signal.event is CaseEvent.HUMAN_TAKEOVER:
            self._emit(case, EventKind.SYNC_TAKEOVER, change.user, detail)
            self._supersede_pending(case)
        else:
            self._emit(case, EventKind.SYNC, change.user, detail)
        case["status"] = signal.after.value
        case["resolved_ts"] = resolved_ts
        return True

    # --- explicit hand-back (UI / API) -----------------------------------------

    def hand_back(self, case_id: str, actor_id: str, *, grants: Collection[RoleGrant]) -> None:
        """``human_owned → in_progress`` by a person (ARCH §8.2).

        The person needs ``hand_back`` over the case's service (WP-103).
        """
        with self._uow:
            case = self._case(case_id)
            service = case.get("service")
            scope = service_role_scope(str(service) if service is not None else None)
            if not can(grants, Permission.HAND_BACK, scope):
                raise AuthorizationDenied(  # noqa: TRY003
                    f"your role doesn't allow handing back cases of {service or 'unresolved'!r}",
                    code="rbac",
                )
            current = CaseStatus(str(case["status"]))
            if current is not CaseStatus.HUMAN_OWNED:
                raise DomainValidationError(  # noqa: TRY003
                    f"only a human-owned case can be handed back; this one is {current.value}"
                )
            after = next_status(current, CaseEvent.HAND_BACK)
            self._cas(case_id, current, after, _keep(case))
            self._emit(
                case,
                EventKind.GATE,
                actor_id,
                {"v": 1, "transition": "hand_back", "from": current.value, "to": after.value},
            )
            self._uow.commit()

    # --- a ticketless outcome (UI, ADR-0024) -------------------------------------

    def record_outcome(  # noqa: PLR0913 — the person, their grants and the outcome
        self,
        case_id: str,
        actor_id: str,
        *,
        grants: Collection[RoleGrant],
        labels: Collection[str],
        classification: str,
        severity: Severity,
        cause: Literal["right", "wrong"] | None,
        drill: bool,
    ) -> None:
        """A person resolves a case with no ticket and records its outcome.

        ``labels`` are the service's taxonomy labels: the outcome is one of them. The
        person needs ``record_outcome`` over the case's service.
        """
        if self._feedback is None:
            raise DomainValidationError("outcomes aren't recorded here")  # noqa: TRY003
        with self._uow:
            case = self._case(case_id)
            service = case.get("service")
            scope = service_role_scope(str(service) if service is not None else None)
            if not can(grants, Permission.RECORD_OUTCOME, scope):
                raise AuthorizationDenied(  # noqa: TRY003
                    f"your role doesn't allow resolving cases of {service or 'unresolved'!r}",
                    code="rbac",
                )
            if case.get("ticket_number"):
                raise DomainValidationError(  # noqa: TRY003
                    "this case has a ticket: it resolves when the ticket does"
                )
            if classification not in labels:
                raise DomainValidationError(  # noqa: TRY003
                    f"{classification!r} isn't a label of this case's service"
                )
            current = CaseStatus(str(case["status"]))
            after = next_status(current, CaseEvent.RESOLVED)
            self._cas(case_id, current, after, int(self._clock.now().timestamp()))
            self._emit(
                case,
                EventKind.GATE,
                actor_id,
                {
                    "v": 1,
                    "transition": "resolved",
                    "from": current.value,
                    "to": after.value,
                    "outcome": classification,
                    "drill": drill,
                },
            )
            self._feedback.record_case_outcome(
                case_id,
                resolved_by=actor_id,
                classification=classification,
                severity=severity,
                cause=cause,
                drill=drill,
            )
            self._uow.commit()

    # --- helpers ----------------------------------------------------------------

    def _case(self, case_id: str) -> dict[str, object]:
        row = (
            self._uow.connection.execute(
                sa.text(
                    "SELECT id, status, fingerprint, trace_id, resolved_ts, service, ticket_number "
                    "FROM cases "
                    "WHERE id = :id AND tenant_id = :tenant"
                ),
                {"id": case_id, "tenant": self._tenant},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound(f"case not found: {case_id!r}")  # noqa: TRY003
        return dict(row)

    def _cas(
        self, case_id: str, before: CaseStatus, after: CaseStatus, resolved_ts: int | None
    ) -> None:
        moved = self._uow.connection.execute(
            sa.text(
                "UPDATE cases SET status = :after, resolved_ts = :resolved "
                "WHERE id = :id AND tenant_id = :tenant AND status = :before"
            ),
            {
                "after": after.value,
                "before": before.value,
                "resolved": resolved_ts,
                "id": case_id,
                "tenant": self._tenant,
            },
        ).rowcount
        if moved != 1:
            raise ConcurrencyConflict(  # noqa: TRY003
                f"case {case_id} is no longer {before.value}; transition to {after.value} lost"
            )

    def _fingerprint_taken(self, case: dict[str, object]) -> bool:
        return (
            self._uow.connection.execute(
                sa.text(
                    "SELECT 1 FROM cases WHERE tenant_id = :tenant AND fingerprint = :fp "
                    "AND status <> 'resolved' AND id <> :id"
                ),
                {"tenant": self._tenant, "fp": case["fingerprint"], "id": case["id"]},
            ).first()
            is not None
        )

    def _supersede_pending(self, case: dict[str, object]) -> None:
        conn = self._uow.connection
        pending = (
            conn.execute(
                sa.text(
                    "SELECT id FROM proposals WHERE case_id = :case AND tenant_id = :tenant "
                    "AND status = 'pending_approval'"
                ),
                {"case": case["id"], "tenant": self._tenant},
            )
            .scalars()
            .all()
        )
        for proposal_id in pending:
            conn.execute(
                sa.text(
                    "UPDATE proposals SET status = :to "
                    "WHERE id = :id AND status = 'pending_approval'"
                ),
                {"to": _SUPERSEDED.value, "id": proposal_id},
            )
            self._emit(
                case,
                EventKind.GATE,
                "agent",
                {
                    "v": 1,
                    "transition": "superseded",
                    "proposal_id": str(proposal_id),
                    "reason": "human_takeover",
                },
            )

    def _automation_actors(self) -> frozenset[str]:
        return confirmed_automation_actors(self._uow.connection, self._tenant)

    def _emit(
        self, case: dict[str, object], kind: EventKind, actor: str, payload: dict[str, object]
    ) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        self._events.append(
            Event(
                id=event_id,
                tenant_id=self._tenant,
                trace_id=str(case["trace_id"]),
                span_id=f"lifecycle_{event_id}",
                parent_span_id=None,
                kind=kind,
                actor=actor,
                payload={**payload, "case_id": str(case["id"])},
                ts=self._clock.now(),
            )
        )


def confirmed_automation_actors(conn: sa.Connection, tenant_id: str) -> frozenset[str]:
    """Confirmed automation actors (``kind = 'human'`` marks a rejected candidate)."""
    return frozenset(
        str(u)
        for u in conn.execute(
            sa.text(
                "SELECT sys_user FROM automation_actors WHERE tenant_id = :tenant "
                "AND confirmed_by IS NOT NULL AND kind <> 'human'"
            ),
            {"tenant": tenant_id},
        ).scalars()
    )


def _keep(case: dict[str, object]) -> int | None:
    value = case.get("resolved_ts")
    return int(str(value)) if value is not None else None


__all__ = ["LifecycleService", "confirmed_automation_actors"]
