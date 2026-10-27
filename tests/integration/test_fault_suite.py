"""WP-106 ``test_fault_injection_suite``: the full fault matrix (ARCH §2 INV-04/05, §19).

Every injectable fault (``SUPPORTAGENT_FAULT_INJECT``) is driven through the
real pipeline piece it hits:

- **LLM faults** (``llm_timeout``, ``llm_garbage``, ``llm_429``,
  ``llm_unavailable``) go through triage: the case safe-defaults
  (``llm-failure``) and nothing is proposed.
- **ServiceNow faults** (``sn_500``, ``sn_429``) go through the outbox
  executor: the row is retried, and nothing reaches the ticket.
- **``crash_after_send``**: the note lands, then the process dies before
  recording it. The next run reconciles through the marker: exactly one
  note (INV-04).

In every case the process stays alive (no fault escapes as a crash except
the simulated one) and there are zero unapproved side effects.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.fakes.faults import FaultInjectingTicketSink, SimulatedCrash
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.adapters.llm.faults import FaultInjectingLLMProvider
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import DEFAULT_LEASE_S, Authorization, OutboxExecutor
from supportagent.domain.faults import LLM_FAULTS, SINK_FAULTS, Fault
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.triage import SafeDefaulted
from supportagent.domain.trace import TraceContext
from supportagent.ports.tickets import NoteVisibility, RecordType
from tests.unit.test_triage_service import _SCOPE, _evt, _llm_json, _seed_grant, _wire


def _count(db: Any, table: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())  # noqa: S608


def test_fault_matrix_covers_every_fault() -> None:
    assert LLM_FAULTS | SINK_FAULTS | {Fault.GHA_500} == set(Fault), (
        "gha_500 acts once the GitHub Actions tool lands (WP-107)"
    )


@pytest.mark.parametrize("fault", sorted(LLM_FAULTS))
def test_fault_injection_suite_llm(  # noqa: PLR0913, PLR0917
    fault: Fault, db: Any, uow: Any, clock: Any, ids: Any, aead: Any
) -> None:
    inner = FakeLLMProvider()
    inner.respond(_llm_json())
    _seed_grant(db)  # a grant that would auto-approve a routed work note
    w = _wire(uow, clock, ids, aead, FaultInjectingLLMProvider(inner, {fault.value}))
    outcome = w.triage.handle(_evt(error_signature=f"5xx:{fault.value}"))
    assert isinstance(outcome, SafeDefaulted), fault
    assert outcome.decision.reason == "llm-failure"
    assert _count(db, "proposals") == 0 and _count(db, "outbox") == 0, "no side effects"


def _queued_note(db: Any, uow: Any, ids: Any, events: Any, sink: Any, clock: Any) -> OutboxExecutor:  # noqa: PLR0913, PLR0917
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts, external_table, external_id, "
                "service, severity, classification) VALUES ('case_f', 'acme', 'fp_f', 'alert', "
                "'triaged', 'tr_f', 0, 0, 0, 'incident', 'INC0000001', 'payments', 'P3', "
                "'payments/api')"
            )
        )
    _seed_grant(db)
    executor = OutboxExecutor(
        uow=uow,
        repo=OutboxRepo(uow),
        clock=clock,
        ids=ids,
        events=events,
        sink=sink,
        handlers=HANDLERS,
    )
    spec = ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(service="payments", system="servicenow", ref="INC0000001"),
        scope=_SCOPE,
        payload={"template_id": "work_note_v1", "text": "work note"},
    )
    executor.enqueue(
        Authorization(policy_grant_id="grnt_seed"),
        spec,
        "case_f",
        TraceContext.new("acme", "tr_f", "span_obx"),
    )
    return executor


@pytest.mark.parametrize("fault", [Fault.SN_500, Fault.SN_429])
def test_fault_injection_suite_servicenow(  # noqa: PLR0913, PLR0917
    fault: Fault, db: Any, uow: Any, clock: Any, ids: Any, aead: Any
) -> None:
    sn = FakeServiceNow()
    sink = FaultInjectingTicketSink(sn, {fault})
    executor = _queued_note(db, uow, ids, SqlEventSink(uow=uow, cipher=aead), sink, clock)
    result = executor.run_once(clock.now())
    assert result.status == "retried", (fault, result)
    assert sn.write_count == 0, "nothing reached the ticket"


def test_fault_injection_suite_crash_after_send(
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any
) -> None:
    sn = FakeServiceNow()
    sink = FaultInjectingTicketSink(sn, {Fault.CRASH_AFTER_SEND})
    executor = _queued_note(db, uow, ids, SqlEventSink(uow=uow, cipher=aead), sink, clock)
    with pytest.raises(SimulatedCrash):
        executor.run_once(clock.now())
    assert sn.write_count == 1, "the note landed before the crash"
    clock.advance(DEFAULT_LEASE_S + 1)  # the dead worker's claim expires
    result = executor.run_once(clock.now())
    assert result.status == "reconciled", result
    assert sn.write_count == 1, "INV-04: the marker prevents a second note"
    assert len(sn.journal_entries(RecordType.INCIDENT, "INC0000001", NoteVisibility.INTERNAL)) == 1
    assert executor.run_once(clock.now()).status == "none"
