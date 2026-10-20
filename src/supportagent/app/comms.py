"""Comms plans and the cadence (ARCH §6.3, §8.2; 01 §7.4, §7.12; 11-wp WP-102).

**``CommsPlanner.plan``** turns a route's ``comms`` plan template into a
``comms_plan`` **proposal** plus its ``comms_plans`` row, in one unit of
work. The proposal's payload is the plan itself, so its ``payload_hash`` is
the ``plan_hash``, and approving it is a real approval: RBAC,
no-self-approval, and the hash the approver saw (``ApprovalService``).

**``CadenceJob.run(now)``** handles each approved plan:

- **active case**: render the current slot (01 §7.12) and enqueue one
  ``customer_comment`` outbox row on the plan path. The idempotency key
  ``plan|id|audience|cadence|slot`` makes a second run in the same slot a
  no-op. A missed slot is not back-filled, because only the current slot is
  rendered.
- **``human_owned`` case**: render the due update as an **owner draft**
  (``comms_drafts``). There is no outbox row and nothing is sent (ARCH §8.2).
- **resolved case**: one final ``resolved`` update, then the plan is
  ``satisfied``.

**Field values.** A ``record_state`` field is the value **a human actor last
set** in the record's per-change history (ARCH §8.2). A value our
integration user wrote never counts. If a field has no human-set value the
slot is skipped and the skip is recorded, rather than sending something the
agent decided. ``schedule`` fields come from the cadence.

The history is read before the write transaction opens, so no database lock
is held across a ServiceNow call.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.app.lifecycle import confirmed_automation_actors
from supportagent.app.outbox import Authorization
from supportagent.domain.comms import PlanSpec, interval_slot, render
from supportagent.domain.errors import AuthorizationDenied, DomainValidationError, NotFound
from supportagent.domain.events import EventKind
from supportagent.domain.idempotency import plan_key
from supportagent.domain.ids import IdPrefix
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.event import Event
from supportagent.domain.model.outbox import OutboxRow
from supportagent.domain.model.proposal import Proposal, Skipped
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.domain.rbac import Permission, RoleGrant, can, service_role_scope
from supportagent.domain.scopes import record_scope
from supportagent.domain.takeover import is_human_actor
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.tickets import SnTable, TicketSource

#: Display values for the record_state fields customers see (ServiceNow defaults).
_STATE_LABELS = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}
_PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low"}
_ACTIVE = frozenset({"open", "triaged", "in_progress", "reopened"})


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
    def submit(self, case_id: str, spec: ActionSpec, trace: TraceContext) -> Proposal | Skipped: ...


class _Writer(Protocol):
    def enqueue(
        self, authorization: Authorization, spec: ActionSpec, case_id: str, trace: TraceContext
    ) -> OutboxRow: ...


def display_value(field: str, raw: str) -> str:
    if field == "state":
        return _STATE_LABELS.get(raw, raw)
    if field == "priority":
        return _PRIORITY_LABELS.get(raw, raw)
    return raw


def human_record_state(
    changes: Sequence[ChangeRecord],
    *,
    integration_user: str,
    automation_actors: Collection[str],
) -> dict[str, str]:
    """Each field's value as **last set by a human actor** (ARCH §6.3, §8.2)."""
    values: dict[str, str] = {}
    for change in sorted(changes, key=lambda c: (c.ts, c.checkpoint or 0)):
        if change.source != "audit" or not change.fieldname or change.newvalue is None:
            continue
        if is_human_actor(
            change.user, integration_user=integration_user, automation_actors=automation_actors
        ):
            values[change.fieldname] = change.newvalue
    return values


class CommsPlanner:
    """A route's ``comms`` template → a ``comms_plan`` proposal + plan row (one UoW)."""

    def __init__(
        self,
        *,
        uow: _UoW,
        ids: IdGenerator,
        gate: _Gate,
        plans: Mapping[str, PlanSpec],
        tenant_id: str,
    ) -> None:
        self._uow = uow
        self._ids = ids
        self._gate = gate
        self._plans = dict(plans)
        self._tenant = tenant_id

    def plan(self, case_id: str, plan_template: str, trace: TraceContext) -> Proposal | Skipped:
        spec = self._plans.get(plan_template)
        if spec is None:
            raise DomainValidationError(f"unknown comms plan template {plan_template!r}")  # noqa: TRY003
        with self._uow:
            case = _case(self._uow.connection, case_id, self._tenant)
            action = ActionSpec(
                kind=ActionKind.COMMS_PLAN,
                target=TargetRef(
                    service=str(case["service"]), system="servicenow", ref=str(case["external_id"])
                ),
                scope=record_scope(
                    "servicenow",
                    str(case["external_table"] or "incident"),
                    str(case["external_id"]),
                    ActionKind.COMMS_PLAN,
                ),
                payload=spec.payload(),
            )
            proposal = self._gate.submit(case_id, action, trace)
            if isinstance(proposal, Proposal):
                self._uow.connection.execute(
                    sa.text(
                        "INSERT INTO comms_plans (id, tenant_id, case_id, audience, channel, "
                        "template_id, allowed_fields_json, cadence_minutes, plan_hash, "
                        "proposal_id, status) VALUES (:id, :t, :c, :aud, :ch, :tpl, :af, :cad, "
                        ":ph, :prop, 'pending_approval')"
                    ),
                    {
                        "id": self._ids.new(IdPrefix.PLAN),
                        "t": self._tenant,
                        "c": case_id,
                        "aud": spec.audience,
                        "ch": spec.channel,
                        "tpl": spec.template_id,
                        "af": json.dumps(spec.fields, sort_keys=True),
                        "cad": spec.cadence_minutes,
                        "ph": spec.plan_hash(),
                        "prop": proposal.id,
                    },
                )
            self._uow.commit()
        return proposal


@dataclass(frozen=True, slots=True)
class CadenceResult:
    plan_id: str
    outcome: Literal["enqueued", "duplicate", "drafted", "skipped", "satisfied", "not_due"]
    detail: str = ""


class CadenceJob:
    """``comms.cadence`` (02 §7, 60 s): render due plan updates."""

    def __init__(  # noqa: PLR0913 — the ports plus the templates and actor identity
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        outbox: _Writer,
        source: TicketSource,
        templates: Mapping[str, str],
        tenant_id: str,
        integration_user: str,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._outbox = outbox
        self._source = source
        self._templates = dict(templates)
        self._tenant = tenant_id
        self._integration_user = integration_user

    def run(self, now: datetime) -> list[CadenceResult]:
        with self._uow:
            plans = self._approved_plans()
            automation = confirmed_automation_actors(self._uow.connection, self._tenant)
        return [self._run_plan(plan, now, automation) for plan in plans]

    # --- one plan -------------------------------------------------------------

    def _run_plan(
        self, plan: dict[str, object], now: datetime, automation: frozenset[str]
    ) -> CadenceResult:
        plan_id = str(plan["id"])
        status = str(plan["case_status"])
        cadence = plan["cadence_minutes"]
        approved_at = datetime.fromtimestamp(int(str(plan["approved_at_ts"])), UTC)
        if status == "resolved":
            trigger, slot = "resolved", 0
        elif cadence is None:
            return CadenceResult(plan_id, "not_due", "no cadence; only the resolved update")
        else:
            trigger, slot = "cadence", interval_slot(now, approved_at, int(str(cadence)))
        key = plan_key(
            plan_id=plan_id, audience=str(plan["audience"]), trigger=trigger, interval_slot=slot
        )
        if status not in ("human_owned", "resolved") and status not in _ACTIVE:
            return CadenceResult(plan_id, "not_due", f"case is {status}")

        # Outside any transaction: the record's history (a ServiceNow read).
        changes = self._source.history(
            SnTable(str(plan["external_table"] or "incident")), str(plan["external_id"]), 0
        )
        state = human_record_state(
            changes, integration_user=self._integration_user, automation_actors=automation
        )
        fields = json.loads(str(plan["allowed_fields_json"]))
        values: dict[str, str] = {}
        for name, source in fields.items():
            if source == "schedule":
                values[name] = (
                    "no further updates (resolved)"
                    if trigger == "resolved"
                    else _eta(approved_at, int(str(cadence)), slot)
                )
            elif name in state:
                values[name] = display_value(name, state[name])
        missing = sorted(set(fields) - set(values))

        with self._uow:
            if missing:
                self._emit(plan, "cadence_skipped", {"missing_human_value": missing, "slot": slot})
                self._uow.commit()
                return CadenceResult(
                    plan_id, "skipped", "no human-set value for " + ", ".join(missing)
                )
            text = render(self._templates[str(plan["template_id"])], values, fields)
            if status == "human_owned":
                result = self._draft(plan, trigger, slot, text)
            else:
                result = self._enqueue(plan, key, trigger, slot, text)
                if trigger == "resolved":
                    self._uow.connection.execute(
                        sa.text("UPDATE comms_plans SET status = 'satisfied' WHERE id = :id"),
                        {"id": plan_id},
                    )
                    self._emit(plan, "plan_satisfied", {})
                    result = CadenceResult(plan_id, "satisfied", result.outcome)
            self._uow.commit()
        return result

    def _enqueue(
        self, plan: dict[str, object], key: str, trigger: str, slot: int, text: str
    ) -> CadenceResult:
        plan_id = str(plan["id"])
        conn = self._uow.connection
        if conn.execute(
            sa.text("SELECT 1 FROM outbox WHERE idempotency_key = :k"), {"k": key}
        ).first():
            return CadenceResult(plan_id, "duplicate", f"slot {slot} already enqueued")
        table = str(plan["external_table"] or "incident")
        sys_id = str(plan["external_id"])
        payload = {"template_id": str(plan["template_id"]), "text": text}
        spec = ActionSpec(
            kind=ActionKind.CUSTOMER_COMMENT,
            target=TargetRef(service=str(plan["service"]), system="servicenow", ref=sys_id),
            scope=record_scope("servicenow", table, sys_id, ActionKind.CUSTOMER_COMMENT),
            payload=payload,
        )
        row = self._outbox.enqueue(
            Authorization(comms_plan_id=plan_id, plan_key=key),
            spec,
            str(plan["case_id"]),
            TraceContext.new(self._tenant, str(plan["trace_id"]), f"comms_{plan_id}_{slot}"),
        )
        conn.execute(
            sa.text(
                "INSERT INTO comms (id, tenant_id, case_id, kind, plan_id, outbox_id, body_json) "
                "VALUES (:id, :t, :c, 'customer_comment', :p, :o, :b)"
            ),
            {
                "id": self._ids.new(IdPrefix.COMMS),
                "t": self._tenant,
                "c": plan["case_id"],
                "p": plan_id,
                "o": row.id,
                "b": json.dumps(payload, sort_keys=True),
            },
        )
        self._emit(
            plan, "cadence_enqueued", {"trigger": trigger, "slot": slot, "outbox_id": row.id}
        )
        return CadenceResult(plan_id, "enqueued", f"{trigger} slot {slot}")

    def _draft(self, plan: dict[str, object], trigger: str, slot: int, text: str) -> CadenceResult:
        plan_id = str(plan["id"])
        conn = self._uow.connection
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM comms_drafts WHERE plan_id = :p AND trigger = :tr "
                "AND interval_slot = :s"
            ),
            {"p": plan_id, "tr": trigger, "s": slot},
        ).first()
        if exists:
            return CadenceResult(plan_id, "duplicate", f"draft for slot {slot} exists")
        conn.execute(
            sa.text(
                "INSERT INTO comms_drafts (id, tenant_id, case_id, plan_id, trigger, "
                "interval_slot, body_json, created_ts) "
                "VALUES (:id, :t, :c, :p, :tr, :s, :b, :ts)"
            ),
            {
                "id": self._ids.new(IdPrefix.COMMS),
                "t": self._tenant,
                "c": plan["case_id"],
                "p": plan_id,
                "tr": trigger,
                "s": slot,
                "b": json.dumps({"text": text}),
                "ts": int(self._clock.now().timestamp()),
            },
        )
        self._emit(plan, "owner_draft", {"trigger": trigger, "slot": slot})
        return CadenceResult(plan_id, "drafted", f"{trigger} slot {slot} drafted for the owner")

    # --- helpers --------------------------------------------------------------

    def _approved_plans(self) -> list[dict[str, object]]:
        rows = self._uow.connection.execute(
            sa.text(
                "SELECT cp.id, cp.case_id, cp.audience, cp.template_id, cp.allowed_fields_json, "
                "cp.cadence_minutes, a.approved_at_ts, c.status AS case_status, c.service, "
                "c.external_table, c.external_id, c.trace_id FROM comms_plans cp "
                "JOIN approvals a ON a.id = cp.approval_id JOIN cases c ON c.id = cp.case_id "
                "WHERE cp.tenant_id = :t AND cp.status = 'approved' ORDER BY cp.id"
            ),
            {"t": self._tenant},
        ).mappings()
        return [dict(r) for r in rows]

    def _emit(self, plan: dict[str, object], phase: str, extra: dict[str, object]) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        self._events.append(
            Event(
                id=event_id,
                tenant_id=self._tenant,
                trace_id=str(plan["trace_id"]),
                span_id=f"comms_{event_id}",
                parent_span_id=None,
                kind=EventKind.SEND,
                actor="agent",
                status="skipped" if phase in ("cadence_skipped", "owner_draft") else "ok",
                payload={"v": 1, "phase": phase, "plan_id": str(plan["id"]), **extra},
                ts=self._clock.now(),
            )
        )


class CommsPlans:
    """Plan reads and revocation for the API / UI (approval goes through ``ApprovalService``)."""

    def __init__(
        self, *, uow: _UoW, clock: Clock, ids: IdGenerator, events: EventSink, tenant_id: str
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._tenant = tenant_id

    def get(self, plan_id: str) -> dict[str, object]:
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT cp.*, c.service, c.trace_id FROM comms_plans cp "
                        "JOIN cases c ON c.id = cp.case_id WHERE cp.id = :id AND cp.tenant_id = :t"
                    ),
                    {"id": plan_id, "t": self._tenant},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise NotFound(f"comms plan not found: {plan_id!r}")  # noqa: TRY003
        plan = dict(row)
        plan["allowed_fields"] = json.loads(str(plan.pop("allowed_fields_json")))
        return plan

    def revoke(self, plan_id: str, actor_id: str, *, grants: Collection[RoleGrant]) -> None:
        """An approved plan stops authorizing sends at once (the trigger checks status)."""
        plan = self.get(plan_id)
        service = plan.get("service")
        if not can(
            grants,
            Permission.DECIDE_PROPOSAL,
            service_role_scope(str(service) if service else None),
        ):
            raise AuthorizationDenied(  # noqa: TRY003
                f"your role doesn't cover service {service!r}", code="rbac"
            )
        with self._uow:
            moved = self._uow.connection.execute(
                sa.text(
                    "UPDATE comms_plans SET status = 'revoked' WHERE id = :id "
                    "AND status IN ('pending_approval', 'approved')"
                ),
                {"id": plan_id},
            ).rowcount
            if not moved:
                raise DomainValidationError(  # noqa: TRY003
                    f"plan {plan_id} is {plan['status']}; only an active plan can be revoked"
                )
            event_id = self._ids.new(IdPrefix.EVENT)
            self._events.append(
                Event(
                    id=event_id,
                    tenant_id=self._tenant,
                    trace_id=str(plan["trace_id"]),
                    span_id=f"comms_{event_id}",
                    parent_span_id=None,
                    kind=EventKind.GATE,
                    actor=actor_id,
                    payload={"v": 1, "transition": "plan_revoked", "plan_id": plan_id},
                    ts=self._clock.now(),
                )
            )
            self._uow.commit()


def _eta(approved_at: datetime, cadence_minutes: int, slot: int) -> str:
    when = approved_at + timedelta(minutes=cadence_minutes * (slot + 1))
    return when.strftime("%Y-%m-%d %H:%M UTC")


def _case(conn: sa.Connection, case_id: str, tenant_id: str) -> dict[str, object]:
    row = (
        conn.execute(
            sa.text(
                "SELECT id, service, external_table, external_id FROM cases "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": case_id, "t": tenant_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFound(f"case not found: {case_id!r}")  # noqa: TRY003
    if row["service"] is None or row["external_id"] is None:
        raise DomainValidationError(  # noqa: TRY003
            f"case {case_id}: a comms plan needs a resolved service and a ticket"
        )
    return dict(row)


__all__ = [
    "CadenceJob",
    "CadenceResult",
    "CommsPlanner",
    "CommsPlans",
    "display_value",
    "human_record_state",
]
