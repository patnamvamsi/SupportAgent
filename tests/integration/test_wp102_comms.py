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
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.adapters.files.comms_loader import load_comms_catalog
from supportagent.app.approvals import ApprovalService
from supportagent.app.comms import CadenceJob, CommsPlanner, CommsPlans
from supportagent.app.gate import GateService
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import Authorization, OutboxExecutor, OutboxWriter
from supportagent.domain.errors import AuthorizationDenied
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.rbac import RoleGrant
from supportagent.domain.roles import Role
from supportagent.domain.trace import TraceContext
from supportagent.ports.tickets import JournalField, SnTable
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
            SnTable.INCIDENT,
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
        return self.sn.journal_entries(SnTable.INCIDENT, _SYS, JournalField.COMMENTS)


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
    sn.seed_record(SnTable.INCIDENT, _SYS, number="INC0000001", sys_updated_on=clock.now())
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
