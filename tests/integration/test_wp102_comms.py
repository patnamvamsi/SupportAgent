"""WP-102 end to end: plan → approval → cadence → outbox → a customer comment.

Real ``GateService``, ``ApprovalService``, ``CommsPlanner``, ``CadenceJob``,
``OutboxWriter`` and ``OutboxExecutor`` run on the test database. The INV-01
trigger checks every send on the plan path. FakeServiceNow is both the
record history the cadence reads and the sink the comment lands on.

Canonical test IDs from 11-wp §WP-102 that live here:
``test_plan_autosend_fields_from_record_state``, ``test_cadence_one_send_per_slot``,
``test_cadence_missed_slot_not_backfilled``, ``test_cadence_human_owned_creates_draft_not_send``,
``test_comms_plan_revoked_blocked``, ``test_resolved_case_satisfies_plan``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.adapters.files.comms_loader import load_comms_catalog
from supportagent.adapters.servicenow.semantics import ServiceNowSemantics
from supportagent.app.approvals import ApprovalService
from supportagent.app.comms import CadenceJob, CommsPlanner, CommsPlans
from supportagent.app.gate import GateService
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import Authorization, OutboxExecutor, OutboxWriter
from supportagent.app.queries import AdminQueries
from supportagent.domain.errors import AuthorizationDenied
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.rbac import RoleGrant
from supportagent.domain.roles import Role
from supportagent.domain.trace import TraceContext
from supportagent.ports.tickets import NoteVisibility, RecordType
from tests.unit.api_support import catalog

_REPO = Path(__file__).resolve().parents[2]
_TENANT = "acme"
_SYS = "INC0000001"
_SVC = "svc_supportagent"
_APPROVER = [RoleGrant(Role.APPROVER)]


@dataclass
class _World:
    db: Any
    clock: Any
    sn: FakeServiceNow
    planner: CommsPlanner
    approvals: ApprovalService
    cadence: CadenceJob
    plans: CommsPlans
    executor: OutboxExecutor
    writer: OutboxWriter
    mod: int = 1

    def history(self, user: str, field: str, value: str) -> None:
        self.clock.advance(1)
        self.mod += 1  # sys_audit rows carry the record checkpoint
        self.sn.seed_audit(
            RecordType.INCIDENT,
            _SYS,
            user=user,
            ts=self.clock.now(),
            fieldname=field,
            newvalue=value,
            checkpoint=self.mod,
        )

    def case_status(self, status: str) -> None:
        with self.db.begin() as conn:
            conn.execute(text("UPDATE cases SET status = :s WHERE id = 'case_c'"), {"s": status})

    def plan_and_approve(self) -> str:
        proposal = self.planner.plan(
            "case_c", "customer-4h", TraceContext.new(_TENANT, "tr_c", "s")
        )
        assert isinstance(proposal, Proposal)
        assert proposal.status is ProposalStatus.PENDING_APPROVAL, "plans are approval only"
        done = self.approvals.approve(
            proposal.id, "sub-bob", seen_hash=proposal.payload_hash, grants=_APPROVER
        )
        assert done.outbox_id is None, "approving a plan sends nothing"
        return str(self.scalar("SELECT id FROM comms_plans"))

    def scalar(self, sql: str) -> Any:
        with self.db.connect() as conn:
            return conn.execute(text(sql)).scalar()

    def comments(self) -> list[str]:
        return self.sn.journal_entries(RecordType.INCIDENT, _SYS, NoteVisibility.CUSTOMER)


@pytest.fixture
def world(db: Any, uow: Any, clock: Any, ids: Any, aead: Any) -> _World:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                "VALUES ('sub-bob', 'acme', 'bob@x', 'Bob', 1, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts, external_table, external_id, service) "
                "VALUES ('case_c', 'acme', 'fp_c', 'ticket', 'triaged', 'tr_c', 0, 0, 0, "
                "'incident', :sys, 'payments')"
            ),
            {"sys": _SYS},
        )
    sn = FakeServiceNow()
    sn.seed_record(RecordType.INCIDENT, _SYS, number="INC0000001", updated_at=clock.now())
    events = SqlEventSink(uow=uow, cipher=aead)
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments"),
        ttl=timedelta(hours=24),
    )
    writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
    comms = load_comms_catalog(_REPO / "templates" / "comms")
    return _World(
        db=db,
        clock=clock,
        sn=sn,
        planner=CommsPlanner(uow=uow, ids=ids, gate=gate, plans=comms.plans, tenant_id=_TENANT),
        approvals=ApprovalService(uow=uow, gate=gate, outbox=writer, tenant_id=_TENANT),
        cadence=CadenceJob(
            uow=uow,
            clock=clock,
            ids=ids,
            events=events,
            outbox=writer,
            source=sn,
            templates=comms.templates,
            tenant_id=_TENANT,
            integration_user=_SVC,
            semantics=ServiceNowSemantics(),
        ),
        plans=CommsPlans(uow=uow, clock=clock, ids=ids, events=events, tenant_id=_TENANT),
        executor=OutboxExecutor(
            uow=uow,
            repo=OutboxRepo(uow),
            clock=clock,
            ids=ids,
            events=events,
            sink=sn,
            handlers=HANDLERS,
        ),
        writer=writer,
    )


def test_cadence_one_send_per_slot(world: _World) -> None:
    world.plan_and_approve()
    world.history("bob", "state", "2")
    world.history("bob", "priority", "3")
    assert [r.outcome for r in world.cadence.run(world.clock.now())] == ["enqueued"]
    assert [r.outcome for r in world.cadence.run(world.clock.now())] == ["duplicate"]
    assert world.scalar("SELECT COUNT(*) FROM outbox") == 1
    assert world.executor.run_once(world.clock.now()).status == "sent"
    (comment,) = world.comments()
    assert "status In Progress, priority 3 - Moderate" in comment
    assert comment.splitlines()[-1].startswith("[sa:"), "the INV-04 marker"
    assert world.scalar("SELECT COUNT(*) FROM comms") == 1
    # BL-034: the comms row follows its outbox outcome (ARCH §6.3 history).
    assert world.scalar("SELECT status FROM comms") == "sent"
    assert world.scalar("SELECT sent_at_ts FROM comms") is not None
    # BL-004: the case view lists the update with its outcome.
    view = AdminQueries(uow=SqlUnitOfWork(world.db), tenant_id="acme").case("case_c")
    assert [(m["status"], m["attempts"]) for m in view.comms] == [("sent", 0)]
    assert "status In Progress" in str(view.comms[0]["text"])


def test_cadence_missed_slot_not_backfilled(world: _World) -> None:
    world.plan_and_approve()
    world.history("bob", "state", "2")
    world.history("bob", "priority", "3")
    world.cadence.run(world.clock.now())  # slot 0
    world.clock.advance(13 * 3600)  # slots 1 and 2 missed; now slot 3
    assert [r.detail for r in world.cadence.run(world.clock.now())] == ["cadence slot 3"]
    assert world.scalar("SELECT COUNT(*) FROM outbox") == 2, "only the current slot is sent"


def test_plan_autosend_fields_from_record_state(world: _World) -> None:
    world.plan_and_approve()
    world.history("bob", "state", "2")
    world.history(_SVC, "state", "6")  # the agent "resolving" never reaches a customer
    (skipped,) = world.cadence.run(world.clock.now())
    assert skipped.outcome == "skipped" and "priority" in skipped.detail, (
        "no human-set priority: the slot is skipped, not filled in"
    )
    world.history("carol", "priority", "2")
    (sent,) = world.cadence.run(world.clock.now())
    assert sent.outcome == "enqueued"
    world.executor.run_once(world.clock.now())
    (comment,) = world.comments()
    assert "status In Progress" in comment and "Resolved" not in comment


def test_cadence_human_owned_creates_draft_not_send(world: _World) -> None:
    world.plan_and_approve()
    world.history("bob", "state", "2")
    world.history("bob", "priority", "3")
    world.case_status("human_owned")
    assert [r.outcome for r in world.cadence.run(world.clock.now())] == ["drafted"]
    assert [r.outcome for r in world.cadence.run(world.clock.now())] == ["duplicate"]
    assert world.scalar("SELECT COUNT(*) FROM outbox") == 0, "nothing is sent"
    assert world.scalar("SELECT COUNT(*) FROM comms_drafts") == 1
    assert "In Progress" in str(world.scalar("SELECT body_json FROM comms_drafts"))


def test_comms_plan_revoked_blocked(world: _World) -> None:
    plan_id = world.plan_and_approve()
    world.history("bob", "state", "2")
    world.history("bob", "priority", "3")
    world.plans.revoke(plan_id, "sub-bob", grants=_APPROVER)
    assert world.cadence.run(world.clock.now()) == [], "a revoked plan renders nothing"
    # Even a direct enqueue on the plan path is refused at the database (INV-01).
    spec = ActionSpec(
        kind=ActionKind.CUSTOMER_COMMENT,
        target=TargetRef(service="payments", system="servicenow", ref=_SYS),
        scope=f"servicenow:incident:{_SYS}:customer_comment",
        payload={"template_id": "customer_update_v1", "text": "hi"},
    )
    with pytest.raises(AuthorizationDenied):
        world.writer.enqueue(
            Authorization(comms_plan_id=plan_id, plan_key="k" * 32),
            spec,
            "case_c",
            TraceContext.new(_TENANT, "tr_c", "s"),
        )
    assert world.scalar("SELECT COUNT(*) FROM outbox") == 0


def test_resolved_case_satisfies_plan(world: _World) -> None:
    world.plan_and_approve()
    world.history("bob", "priority", "3")
    world.history("bob", "state", "6")
    world.case_status("resolved")
    (final,) = world.cadence.run(world.clock.now())
    assert final.outcome == "satisfied"
    assert world.scalar("SELECT status FROM comms_plans") == "satisfied"
    world.executor.run_once(world.clock.now())
    (comment,) = world.comments()
    assert "status Resolved" in comment and "no further updates" in comment
    assert world.cadence.run(world.clock.now()) == [], "a satisfied plan sends nothing more"


def test_plan_is_never_grant_approved(world: _World, db: Any) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                "requested_by, requested_at_ts, granted_by, granted_at_ts, valid_to_ts) "
                "VALUES ('g1', 'acme', "
                "'comms_plan', :s, 'approved', 'config:test', 1, 'sub-bob', 1, 9999999999)"
            ),
            {"s": f"servicenow:incident:{_SYS}:comms_plan"},
        )
    proposal = world.planner.plan("case_c", "customer-4h", TraceContext.new(_TENANT, "tr_c", "s"))
    assert isinstance(proposal, Proposal)
    assert proposal.status is ProposalStatus.PENDING_APPROVAL


def _human_created(world: _World, creator: str = "ada") -> None:
    """ADR-0010: the record as a person raised it (state New, priority 3)."""
    world.sn.seed_record(
        RecordType.INCIDENT,
        _SYS,
        number="INC0000001",
        state="1",
        priority="3",
        updated_at=world.clock.now(),
        created_by=creator,
    )


def test_cadence_sends_the_values_a_human_set_at_creation(world: _World) -> None:
    """ADR-0010: no human change yet, yet the human creator's values count.

    Before ADR-0010 this slot was skipped, and a human changing ``state`` is
    a takeover, so an active ServiceNow case could never get an update.
    """
    _human_created(world)
    world.plan_and_approve()
    (sent,) = world.cadence.run(world.clock.now())
    assert sent.outcome == "enqueued", sent.detail
    world.executor.run_once(world.clock.now())
    (comment,) = world.comments()
    assert "status New, priority 3 - Moderate" in comment


def test_creation_values_do_not_count_when_the_agent_created_the_record(world: _World) -> None:
    _human_created(world, creator=_SVC)
    world.plan_and_approve()
    (skipped,) = world.cadence.run(world.clock.now())
    assert skipped.outcome == "skipped"


def test_creation_value_is_the_first_changes_old_value(world: _World) -> None:
    """A field changed since creation: its creation value is the first ``oldvalue``."""
    _human_created(world)
    world.plan_and_approve()
    world.clock.advance(1)
    world.mod += 1
    world.sn.seed_audit(
        RecordType.INCIDENT,
        _SYS,
        user=_SVC,
        ts=world.clock.now(),
        fieldname="priority",
        oldvalue="4",
        newvalue="1",
        checkpoint=world.mod,
    )
    (sent,) = world.cadence.run(world.clock.now())
    assert sent.outcome == "enqueued"
    world.executor.run_once(world.clock.now())
    (comment,) = world.comments()
    assert "priority 4 - Low" in comment, "the agent's priority 1 never reaches a customer"
