"""ADR-0014 part 2 on the database: SOP auto mode is a conditioned grant (BL-054).

The ``rca.run`` job submits an applicable SOP step with the RCA's tool
results as evidence. An approved grant on the step's scope sends it without
a per-action approval only when its conditions hold; anything else (a check
that didn't hold, no conditions, unreadable conditions, the daily cap, a
revoked grant) leaves it to a person, and the gate event records why. A sent
step can be retracted: the retraction is a proposal a person approves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.approvals import ApprovalService
from supportagent.app.gate import GateService
from supportagent.app.outbox import OutboxWriter
from supportagent.app.queries import AdminQueries, auto_timeline
from supportagent.app.rca import BufferedEvents, DocPolicy, Knowledge, RcaJob, RcaService
from supportagent.app.setup import SetupService
from supportagent.domain.auto import AutoConditions
from supportagent.domain.errors import ConcurrencyConflict, DomainValidationError
from supportagent.domain.events import EventKind
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.rbac import RoleGrant
from supportagent.domain.roles import Role
from supportagent.domain.slots import SlotName
from tests.integration.test_sop_requests_db import _SOP, _docs
from tests.integration.test_wp110_rca import _ROUTED
from tests.unit.test_rca import (
    FAILED_RUN,
    POOL_LOGS,
    ROOT,
    SERVICE,
    FakeTools,
    ScriptedLLM,
    _knowledge,
)

_SCOPE = f"sop:wiki:deploy-sop.md@{_SOP.digest}:roll-back:work_note"
_APPROVER = [RoleGrant(Role.APPROVER)]


@dataclass
class _World:
    job: RcaJob
    approvals: ApprovalService
    setup: SetupService
    events: SqlEventSink
    uow: SqlUnitOfWork


def _world(
    db: Engine, master_key: bytes, clock: Any, ids: Any, *, verdict: str = "supports"
) -> _World:
    uow = SqlUnitOfWork(db)
    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    events = SqlEventSink(uow=uow, cipher=aead)
    buffer = BufferedEvents()
    kb = _knowledge(ROOT / "runbooks")
    catalog = Catalog(version="1", services=[SERVICE.model_copy(update={"docs": ["wiki"]})])
    service = RcaService(
        knowledge=Knowledge(kb.runbooks, kb.probes, kb.excludes),
        tools=FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS}),
        llm=ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": verdict}}),
        events=buffer,
        ids=ids,
        clock=clock,
        prior={},
        docs=_docs(),
        doc_policy=DocPolicy(),
    )
    writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog,
        ttl=timedelta(hours=4),
        outbox=writer,
    )
    job = RcaJob(
        uow=uow,
        service=service,
        catalog=catalog,
        buffer=buffer,
        events=events,
        lite_routes={"db-saturation"},
        default_budget=RcaBudget(),
        ids=ids,
        clock=clock,
        tenant_id="acme",
        gate=gate,
    )
    setup = SetupService(
        uow=uow, clock=clock, ids=ids, events=events, catalog=catalog, tenant_id="acme"
    )
    approvals = ApprovalService(uow=uow, gate=gate, outbox=writer, tenant_id="acme")
    return _World(job, approvals, setup, events, uow)


def _seed(db: Engine, case_id: str = "case_1") -> None:
    with db.begin() as conn:
        for uid in ("user_ada", "user_bo"):
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                    "SELECT :id, 'acme', :e, :id, 1, 0 WHERE NOT EXISTS "
                    "(SELECT 1 FROM users WHERE id = :id)"
                ),
                {"id": uid, "e": f"{uid}@acme.test"},
            )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, classification, "
                "severity, status, decision_json, opened_ts, last_alert_ts, trace_id, "
                "external_table, external_id) "
                "VALUES (:id, 'acme', :fp, 'alert', 'payroll-api', 'database/saturation', 'P2', "
                "'triaged', :d, 100, 100, :tr, 'incident', :ext)"
            ),
            {
                "id": case_id,
                "fp": f"fp-{case_id}",
                "d": _ROUTED,
                "tr": f"tr-{case_id}",
                "ext": f"sys-{case_id}",
            },
        )


def _admin(db: Engine, user_id: str) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO role_assignments (tenant_id, user_id, role, scope, granted_by, "
                "granted_ts) VALUES ('acme', :u, 'admin', '*', 'user_ada', 0)"
            ),
            {"u": user_id},
        )


def _grant(db: Engine, conditions: str | None) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                "requested_by, requested_at_ts, granted_by, granted_at_ts, valid_to_ts, "
                "conditions_json) VALUES ('grnt_auto', 'acme', 'work_note', :scope, 'approved', "
                "'cli:sop-auto', 0, 'user_bo', 0, 9999999999, :c)"
            ),
            {"scope": _SCOPE, "c": conditions},
        )


def _conditions(*checks: str, cap: int = 5, services: tuple[str, ...] = ()) -> str:
    return AutoConditions(checks=checks, max_per_day=cap, services=services).to_json()


def _sop_proposal(db: Engine, case_id: str = "case_1") -> tuple[str, str]:
    with db.connect() as conn:
        row = conn.execute(
            text("SELECT id, status FROM proposals WHERE scope = :s AND case_id = :c"),
            {"s": _SCOPE, "c": case_id},
        ).one()
    return str(row[0]), str(row[1])


def _gate_autos(w: _World, trace_id: str = "tr-case_1") -> list[dict[str, Any]]:
    with w.uow:
        found = w.events.trace_events(trace_id, "acme")
    out: list[dict[str, Any]] = []
    for e in found:
        auto = e.payload.get("auto")
        if e.kind is EventKind.GATE and isinstance(auto, dict):
            out.append({**auto, "transition": e.payload["transition"]})
    return out


def _sends(db: Engine) -> list[tuple[str, str | None, str | None]]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT action_kind, policy_grant_id, approval_id FROM outbox ORDER BY id")
        ).all()
    return [(str(r[0]), r[1], r[2]) for r in rows]


def test_conditions_hold_so_the_step_is_sent_on_the_grant(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed(db)
    _grant(db, _conditions("recent_change_push", services=("payroll-api",)))
    w = _world(db, master_key, clock, ids)

    assert w.job.run_once() == 1

    _, status = _sop_proposal(db)
    assert status == "executed"
    assert _sends(db) == [("work_note", "grnt_auto", None)]  # INV-01 path 2: the grant
    [record] = _gate_autos(w)
    assert record["transition"] == "grant_enqueued" and record["refused"] == []
    assert record["grant_id"] == "grnt_auto" and record["used_today"] == 0
    assert record["conditions"]["checks"] == ["recent_change_push"]
    assert record["evidence"]["confirmed"] is True
    assert "recent_change_push" in record["evidence"]["positive"]
    with db.connect() as conn:
        raw = conn.execute(
            text("SELECT payload_json FROM proposals WHERE scope = :s"), {"s": _SCOPE}
        ).scalar_one()
    payload = json.loads(raw)
    assert payload["revert"] == "retraction_note" and payload["rca_artifact"].startswith("rca")


@pytest.mark.parametrize(
    ("conditions", "verdict", "why"),
    [
        (_conditions("db_locks"), "supports", "checks not positive: ['db_locks']"),
        (_conditions("recent_change_push"), "unknown", "did not pass the evidence check"),
        (_conditions("recent_change_push", services=("billing",)), "supports", "not in"),
        (None, "supports", "without conditions covers nothing"),
        ("{not json", "supports", "not JSON"),
        (
            '{"checks":["recent_change_push"],"max_per_day":5,"min_score":0.9}',
            "supports",
            "calibrated thresholds",
        ),
    ],
)
def test_a_condition_that_fails_or_cant_be_read_leaves_the_step_to_a_person(  # noqa: PLR0913, PLR0917
    db: Engine,
    master_key: bytes,
    clock: Any,
    ids: Any,
    conditions: str | None,
    verdict: str,
    why: str,
) -> None:
    _seed(db)
    _grant(db, conditions)
    w = _world(db, master_key, clock, ids, verdict=verdict)

    assert w.job.run_once() == 1

    _, status = _sop_proposal(db)
    assert status == "pending_approval"
    assert _sends(db) == []
    [record] = _gate_autos(w)
    assert record["transition"] == "auto_declined"
    assert any(why in r for r in record["refused"]), record["refused"]


def test_the_daily_cap_and_a_revoked_grant_stop_auto_mode(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed(db, "case_1")
    _seed(db, "case_2")
    _grant(db, _conditions("recent_change_push", cap=1))
    w = _world(db, master_key, clock, ids)
    assert w.job.run_once() == 2  # both cases due; the cap allows one auto send

    statuses = sorted(_sop_proposal(db, c)[1] for c in ("case_1", "case_2"))
    assert statuses == ["executed", "pending_approval"]
    declined = [
        r
        for trace in ("tr-case_1", "tr-case_2")
        for r in _gate_autos(w, trace)
        if r["transition"] == "auto_declined"
    ]
    assert len(declined) == 1 and "rate cap reached: 1 of 1" in declined[0]["refused"][0]

    _admin(db, "user_ada")
    w.setup.revoke_grant("grnt_auto", actor_id="user_ada")
    _seed(db, "case_3")
    assert w.job.run_once() == 1
    assert _sop_proposal(db, "case_3")[1] == "pending_approval"  # revoked: stops at once


def test_a_sent_step_is_retracted_through_an_approval(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed(db)
    _grant(db, _conditions("recent_change_push"))
    w = _world(db, master_key, clock, ids)
    w.job.run_once()
    sent, _ = _sop_proposal(db)

    retraction = w.approvals.retract(sent, "user_ada", grants=_APPROVER)
    with pytest.raises(ConcurrencyConflict):
        w.approvals.retract(sent, "user_ada", grants=_APPROVER)

    queries = AdminQueries(uow=SqlUnitOfWork(db), tenant_id="acme")
    p = queries.proposal(retraction)
    assert p.status == "pending_approval" and p.scope == f"retract:{sent}:work_note"
    assert p.payload["retracts"] == sent and p.payload["template_id"] == "retraction_v1"
    assert "withdrawn; disregard it. Retracted by user_ada." in p.payload["text"]
    w.approvals.approve(retraction, "user_ada", seen_hash=p.payload_hash, grants=_APPROVER)
    sends = _sends(db)
    assert [s[0] for s in sends] == ["work_note", "work_note"]
    assert sends[1][2] is not None  # the retraction went out on its approval

    view = queries.case("case_1")
    with w.uow:
        trace = w.events.trace_events("tr-case_1", "acme")
    [entry] = auto_timeline(view, trace)
    assert entry["auto"] is True and entry["proposal_id"] == sent
    assert entry["retraction_id"] == retraction and entry["can_retract"] is False
    assert entry["send_status"] == "pending"  # the executor doesn't run in this test


def test_only_an_executed_action_that_declares_a_revert_is_retracted(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed(db)
    w = _world(db, master_key, clock, ids)
    w.job.run_once()  # no auto grant: the step waits for a person
    pending, _ = _sop_proposal(db)
    with pytest.raises(DomainValidationError, match="only a sent action"):
        w.approvals.retract(pending, "user_ada", grants=_APPROVER)


def test_request_sop_auto_validates_and_stores_the_conditions(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed(db)
    w = _world(db, master_key, clock, ids)
    conditions = AutoConditions(checks=("recent_change_push",), max_per_day=3)
    for bad, match in (
        ("service:payroll-api:work_note", "not an SOP step scope"),
        (_SCOPE.replace(":work_note", ":job_rerun"), "declares no revert"),
        (_SCOPE.replace(":work_note", ":nope"), "unknown kind"),
    ):
        with pytest.raises(DomainValidationError, match=match):
            w.setup.request_sop_auto(bad, conditions, requested_by="cli:sop-auto")
    with pytest.raises(DomainValidationError, match="unknown service"):
        w.setup.request_sop_auto(
            _SCOPE,
            AutoConditions(checks=("x",), max_per_day=1, services=("nope",)),
            requested_by="cli:sop-auto",
        )

    grant_id = w.setup.request_sop_auto(_SCOPE, conditions, requested_by="cli:sop-auto")
    with pytest.raises(ConcurrencyConflict):
        w.setup.request_sop_auto(_SCOPE, conditions, requested_by="cli:sop-auto")
    _admin(db, "user_bo")
    w.setup.approve_grant(grant_id, approver_id="user_bo", valid_days=30)
    [row] = AdminQueries(uow=SqlUnitOfWork(db), tenant_id="acme").grants(status="approved")
    assert row["scope"] == _SCOPE
    assert row["conditions"] == {
        "checks": ["recent_change_push"],
        "max_per_day": 3,
        "services": [],
    }

    assert w.job.run_once() == 1
    assert _sop_proposal(db)[1] == "executed"
