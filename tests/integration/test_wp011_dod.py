"""WP-011 DoD: end-to-end Loop A integration test (10-wp §WP-011).

Spec (docs/tech-spec/10-wp-v0.0.md:150) verbatim:

    **DoD:** an end-to-end integration test with FakeServiceNow → work note
    posted once, replay shows the full path.

This is the *new integration leg* called out by the WP-011 objective, distinct
from the 8 frozen unit tests in ``tests/unit/test_triage_service.py``. It
drives the full Loop A pipeline against a real (SQLite) DB and a fake
ServiceNow sink (``integration`` marker: "use case + real DB + fake externals
through bootstrap", 04 §1):

1. An inbound alert is ingested, deduped, pre-classified, classified (LLM),
   decided, and routed to a ``WORK_NOTE`` proposal (one unit of work).
2. A setup policy grant auto-approves the proposal through the ``GateService``
   (no human approval row — grant path, INV-01).
3. The approved proposal is enqueued into the outbox under the grant's
   ``Authorization`` (idempotency key derived from the grant).
4. The ``OutboxExecutor`` claims the row and delivers the work note to
   ``FakeServiceNow`` exactly once (INV-04 effectively-once).
5. A replay of the trace shows the full path (INGEST → DEDUP → PRECLASS →
   REDACT → LLM → DECISION → GATE).

The test is marked ``integration`` (not ``sn``) because it uses
``FakeServiceNow`` — no real ServiceNow host is required, so it is safe to run
in CI without the ``record-sn`` / ``--allow-hosts`` gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.llm.client import LLMClient, SlotSpec
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.app.classify import Classifier
from supportagent.app.gate import GateService
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import Authorization, OutboxExecutor
from supportagent.app.replay import ReplayService
from supportagent.app.triage import TriageService
from supportagent.domain.events import EventKind
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.triage import Triaged
from supportagent.domain.preclass import RuleSet
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.llm import LLMProvider
from supportagent.ports.tickets import JournalField, SnTable
from tests.builders import an_alert

pytestmark = pytest.mark.integration

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

_MODEL = "claude-sonnet-4-5-20250929"
_TTL = timedelta(hours=24)
_LABEL = "payments/api"
_STRONG = CalibrationCountRow(n=40, correct=40)

TENANT = "acme"
SYS_ID = "INC0000001"
_GRANT_ID = "grnt_dod"
# ADR-0005: T1 kinds are scoped to the catalog service, not the record.
_SCOPE = "service:payments:work_note"


# --- shared wiring (mirrors tests/unit/test_triage_service.py) ---------------


def _catalog() -> Catalog:
    return Catalog(
        version="1",
        services=[
            ServiceEntry(
                name="payments",
                repos=["acme/payments"],
                log_paths=["/var/log/payments"],
                dags=[],
                cmdb_ci=["CIPAYMENTS"],
                resources=["payments-api"],
                owners=["team-payments"],
                depends_on=[],
            ),
        ],
    )


def _table() -> CalibrationTable:
    return CalibrationTable(
        grade="smoke",
        snapshot_id="snap-1",
        model_id=_MODEL,
        labels={_LABEL: CalibrationLabelRow(agree=_STRONG, abstain=_STRONG)},
    )


def _routing() -> RoutingTable:
    return RoutingTable(
        rules=[
            RoutingRule(
                match_classification=_LABEL,
                severities=[Severity.P1, Severity.P2, Severity.P3, Severity.P4],
                route=Route(id="payments-route", actions=[ActionKind.WORK_NOTE]),
            )
        ],
        default=Route(id="generic", actions=[ActionKind.WORK_NOTE]),
    )


def _llm_json() -> str:
    return (
        '{"classification": "payments/api", "severity": "P3",'
        ' "summary": "payments api failure", "confidence": 0.95}'
    )


@dataclass
class _Wired:
    triage: TriageService
    events: SqlEventSink
    gate: GateService
    case_repo: CaseRepo
    uow: SqlUnitOfWork
    catalog: Catalog
    fake: LLMProvider


def _wire(
    uow: SqlUnitOfWork,
    clock: Any,
    ids: Any,
    aead: Any,
    fake: LLMProvider,
) -> _Wired:
    events = SqlEventSink(uow=uow, cipher=aead)
    case_repo = CaseRepo(uow)
    catalog = _catalog()
    llm_client = LLMClient(
        provider=fake,
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=PromptRegistry(root=_REPO_ROOT / "prompts"),
        events=events,
        ids=ids,
        clock=clock,
        slots={SlotName.CLASSIFY: SlotSpec(model_id=_MODEL, timeout_s=15.0, max_output_tokens=400)},
    )
    classifier = Classifier(
        rule_set=RuleSet(rules=[]),
        llm=llm_client,
        calibration=_table(),
        min_score=0.80,
        routing=_routing(),
    )
    gate = GateService(uow=uow, clock=clock, ids=ids, events=events, catalog=catalog, ttl=_TTL)
    triage = TriageService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=case_repo,
        classifier=classifier,
        gate=gate,
        catalog=catalog,
    )
    return _Wired(triage, events, gate, case_repo, uow, catalog, fake)


def _evt(**overrides: object) -> AlertEvent:
    base = an_alert()
    base.update(overrides)
    return AlertEvent.model_validate(base)


def _seed_grant(db: Any, *, scope: str = _SCOPE, grant_id: str = _GRANT_ID) -> None:
    """An approved, in-scope, unexpired policy grant (trigger path 2)."""
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                "VALUES ('user_ada', 'acme', 'ada@acme.test', 'Ada', 1, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                "VALUES ('user_bo', 'acme', 'bo@acme.test', 'Bo', 1, 0)"
            )
        )
        # text(): named parameters bind on both engines (exec_driver_sql would
        # pass ":id" straight to psycopg, which only knows "%(id)s").
        conn.execute(
            text(
                "INSERT INTO policy_grants "
                "(id, tenant_id, action_kind, scope, status, requested_by, requested_at_ts, "
                "granted_by, granted_at_ts, valid_to_ts) "
                "VALUES (:id, 'acme', 'work_note', :scope, 'approved', 'user_ada', 0, "
                "'user_bo', 0, 9999999999)"
            ),
            {"id": grant_id, "scope": scope},
        )


def _executor(
    uow: Any,
    clock: Any,
    ids: Any,
    events: Any,
    fake_sn: Any,
) -> OutboxExecutor:
    return OutboxExecutor(
        uow=uow,
        repo=OutboxRepo(uow),
        clock=clock,
        ids=ids,
        events=events,
        sink=fake_sn,
        handlers=HANDLERS,
    )


# --- the DoD integration test ------------------------------------------------


def test_wp011_dod_work_note_posted_once_and_replayed(  # noqa: PLR0913, PLR0917
    db: Any,
    uow: Any,
    clock: Any,
    ids: Any,
    aead: Any,
    fake_llm: Any,
    fake_sn: Any,
) -> None:
    """WP-011 DoD: alert → triage → grant-approve → outbox → work note once.

    The full Loop A path is driven end-to-end against a real SQLite DB and a
    fake ServiceNow sink; the work note lands on the incident exactly once and
    the replay of the trace shows every stage (INGEST … GATE).
    """
    # 1. The LLM classifies the alert to the payments/api service.
    fake_llm.respond(_llm_json())
    # 2. A setup grant auto-approves the work_note proposal (INV-01 grant path).
    _seed_grant(db)
    w = _wire(uow, clock, ids, aead, fake_llm)

    # 3. Triage: ingest + dedup + preclass + llm + decision + gate (one UoW).
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, Triaged)
    assert outcome.proposals, "a routed case must submit at least one proposal"
    # Every proposal is a WORK_NOTE, in scope, and auto-approved by the grant.
    for p in outcome.proposals:
        assert p.action_kind == ActionKind.WORK_NOTE
        assert p.scope == _SCOPE
        assert p.status == ProposalStatus.APPROVED

    proposal = outcome.proposals[0]
    trace_id = proposal.trace_id
    case_id = outcome.case_id

    # 4. Enqueue the approved proposal into the outbox under the grant's
    #    Authorization (idempotency key derived from the grant, not an approval).
    spec = ActionSpec(
        kind=proposal.action_kind,
        target=TargetRef(service="payments", system="servicenow", ref=SYS_ID),
        scope=proposal.scope,
        payload=dict(proposal.payload),
    )
    trace = TraceContext.new(TENANT, trace_id, "span_outbox")
    ex = _executor(uow, clock, ids, w.events, fake_sn)
    ex.enqueue(Authorization(policy_grant_id=_GRANT_ID), spec, case_id, trace)

    # 5. The worker drains the outbox: the work note is delivered once.
    res = ex.run_once(clock.now())
    assert res.status == "sent", f"expected 'sent', got {res.status!r} ({res.error})"

    # 6. The work note landed on the incident exactly once (INV-04).
    assert fake_sn.write_count == 1, (
        f"expected exactly one external write, got {fake_sn.write_count}"
    )
    entries = fake_sn.journal_entries(SnTable.INCIDENT, SYS_ID, JournalField.WORK_NOTES)
    assert len(entries) == 1, f"expected one journal entry, got {len(entries)}"
    # The idempotency marker is the last line of the posted entry (01 §7.5).
    assert entries[0].splitlines()[-1].startswith("[sa:")

    # 7. A second run_once is a no-op (queue drained) — no double-send.
    res2 = ex.run_once(clock.now())
    assert res2.status == "none"
    assert fake_sn.write_count == 1, "a second run must not re-send the work note"

    # 8. Replay of the trace shows the full path (INGEST … GATE).
    with uow:
        view = ReplayService(repo=w.events).replay(trace_id)
    kinds = [e.kind for e in view.events]
    for expected in (
        EventKind.INGEST,
        EventKind.DEDUP,
        EventKind.PRECLASS,
        EventKind.REDACT,
        EventKind.LLM,
        EventKind.DECISION,
        EventKind.GATE,
    ):
        assert expected in kinds, f"expected {expected} in replay, got {kinds}"
