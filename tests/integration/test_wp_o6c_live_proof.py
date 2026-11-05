"""WP-O6c on the database: shadow's RCA proves classes live; resolved cases calibrate.

In shadow mode the ``rca.run`` job also runs on a case held back only until
readiness and calibration catch up. Readiness reads every resolved case back
(``LiveProof``): its live RCA, and what triage predicted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.live_proof import LiveProof
from supportagent.app.rca import BufferedEvents, RcaJob, RcaService
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.slots import SlotName
from tests.unit.test_rca import (
    FAILED_RUN,
    POOL_LOGS,
    ROOT,
    SERVICE,
    FakeTools,
    ScriptedLLM,
    _knowledge,
)


def _decision(outcome: str, reason: str, **extra: object) -> str:
    return json.dumps({"outcome": outcome, "reason": reason, **extra})


def _insert(  # noqa: PLR0913 — one column per argument
    db: Engine,
    case_id: str,
    *,
    opened: int,
    decision: str,
    status: str = "triaged",
    ticket: str | None = None,
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, classification, "
                "severity, status, decision_json, opened_ts, last_alert_ts, trace_id, "
                "ticket_number) VALUES (:id, 'acme', :fp, 'ticket', 'payroll-api', "
                "'database/saturation', 'P3', :st, :d, :ts, :ts, :tr, :tk)"
            ),
            {
                "id": case_id,
                "fp": f"fp-{case_id}",
                "st": status,
                "d": decision,
                "ts": opened,
                "tr": f"tr-{case_id}",
                "tk": ticket,
            },
        )


def _job(db: Engine, master_key: bytes, clock: Any, ids: Any, *, gate: Any = None) -> RcaJob:
    uow = SqlUnitOfWork(db)
    buffer = BufferedEvents()
    return RcaJob(
        uow=uow,
        service=RcaService(
            knowledge=_knowledge(ROOT / "runbooks"),
            tools=FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS}),
            llm=ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": "supports"}}),
            events=buffer,
            ids=ids,
            clock=clock,
            prior={},
        ),
        catalog=Catalog(version="1", services=[SERVICE]),
        buffer=buffer,
        events=SqlEventSink(
            uow=uow, cipher=Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
        ),
        lite_routes=set(),
        default_budget=RcaBudget(),
        ids=ids,
        clock=clock,
        tenant_id="acme",
        gate=gate,
        prove_live=gate is None,
    )


def test_shadow_runs_rca_on_cases_held_back_until_readiness_catches_up(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    held = {
        "case_unready": "class-not-ready",
        "case_lowcal": "low-calibrated-score",
        "case_p1": "unconfirmed-high-severity",
    }
    for i, (case_id, reason) in enumerate(held.items()):
        _insert(db, case_id, opened=100 + i, decision=_decision("safe_default", reason))
    # held back for reasons RCA can't help with: no label to plan from, or no service
    _insert(db, "case_conflict", opened=110, decision=_decision("safe_default", "llm-failure"))
    _insert(db, "case_storm", opened=111, decision=_decision("safe_default", "storm-member"))

    assert _job(db, master_key, clock, ids).run_once() == len(held)
    with db.connect() as conn:
        done = {r[0] for r in conn.execute(text("SELECT case_id FROM rca_artifacts"))}
    assert done == set(held)


def test_only_shadow_proves_live_a_gate_would_propose_from_it(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    with pytest.raises(ValueError, match="shadow mode"):
        RcaJob(
            uow=SqlUnitOfWork(db),
            service=None,  # type: ignore[arg-type] # rejected before it's used
            catalog=Catalog(version="1", services=[SERVICE]),
            buffer=BufferedEvents(),
            events=None,  # type: ignore[arg-type]
            lite_routes=set(),
            default_budget=RcaBudget(),
            ids=ids,
            clock=clock,
            tenant_id="acme",
            gate=object(),  # type: ignore[arg-type]
            prove_live=True,
        )


def test_resolved_cases_are_read_back_with_their_live_rca(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _insert(
        db,
        "case_a",
        opened=100,
        ticket="INC100",
        decision=_decision("safe_default", "class-not-ready", preclass_label=None),
    )
    _insert(
        db,
        "case_b",
        opened=101,
        ticket="INC101",
        decision=_decision("routed", "routed", preclass_label="database/latency"),
    )
    _insert(db, "case_open", opened=102, ticket="INC102", decision=_decision("routed", "routed"))
    assert _job(db, master_key, clock, ids).run_once() == 1  # case_a; case_b had no lite route
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'resolved' WHERE id IN ('case_a', 'case_b')"))

    seen = LiveProof(uow=SqlUnitOfWork(db), tenant_id="acme").outcomes()

    assert set(seen) == {"INC100", "INC101"}  # INC102 isn't resolved: its class isn't known
    a, b = seen["INC100"], seen["INC101"]
    assert a.verdict is not None and a.tested
    assert a.supported is None or a.verdict == "SUPPORTED"
    assert a.classification == "database/saturation" and a.preclass is None
    assert b.verdict is None and b.supported is None  # no RCA: a calibration observation only
    assert b.preclass == "database/latency"
