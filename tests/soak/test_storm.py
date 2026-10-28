"""WP-114: the storm soak gate (ARCH §19, 04 §1; 11-wp WP-114).

1,000 alerts arrive over 5 minutes of simulated time across 40 catalog
services joined by ``depends_on`` chains. 30 % repeat an earlier alert
(duplicates). They flow through the assisted pipeline: dedup, storm grouping,
the spend breaker, classification, the gate with setup grants (T1 work
notes enqueue in the triage UoW), and the outbox executor posting to
FakeServiceNow. The sink is chaotic: every 7th call fails with ``sn_500``,
and three sends "crash" after the write lands (``crash_after_send``).

The run happens once per module; each spec test asserts one property:

- ``test_storm_zero_duplicate_cases``: one case per distinct alert (INV-02);
- ``test_storm_llm_calls_bounded_by_groups``: LLM calls ≤ groups + the cases
  triaged before storm mode began;
- ``test_storm_zero_double_sends``: one work note per outbox row, even across
  the crashes (INV-04 effectively-once);
- ``test_storm_completes_within_budget``: well under the 15-minute 04 §1 budget.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.clock import FrozenClock
from supportagent.adapters.fakes.faults import SimulatedCrash
from supportagent.adapters.fakes.ids import SequentialIdGenerator
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.adapters.llm.client import LLMClient, SlotSpec
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.app.classify import Classifier
from supportagent.app.gate import GateService
from supportagent.app.grouping import GroupingService
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import DEFAULT_LEASE_S, OutboxExecutor, OutboxWriter
from supportagent.app.spend import SpendBreaker
from supportagent.app.triage import TriageService
from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.grouping import STORM_CASES
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.preclass import RuleSet
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from supportagent.ports.tickets import NoteVisibility, RecordType, TicketSink
from tests.conftest import _reset_schema
from tests.unit.test_triage_service import _MODEL, _llm_json, _routing, _table

_REPO = Path(__file__).resolve().parents[2]
ALERTS = 1_000
SERVICES = 40
CHAIN = 5  # services per depends_on chain
DUPLICATE_SHARE = 0.30
WINDOW_S = 300
CRASHES = 3
FAIL_EVERY = 7
T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _catalog() -> Catalog:
    services = []
    for i in range(SERVICES):
        name = f"svc-{i:02d}"
        depends = [f"svc-{i - 1:02d}"] if i % CHAIN else []
        services.append(
            ServiceEntry(
                name=name,
                resources=[f"{name}-api"],
                depends_on=depends,
                paging_integration=f"pagerduty:{name}",
            )
        )
    return Catalog(version="soak", services=services)


def _word(n: int) -> str:
    """``n`` as letters (base 26), so normalization keeps it distinct."""
    out = ""
    while True:
        n, r = divmod(n, 26)
        out = chr(ord("a") + r) + out
        if n == 0:
            return out


def _alerts() -> list[AlertEvent]:
    """Deterministic: a seeded generator, so the soak is reproducible."""
    rnd = random.Random(20261021)  # noqa: S311 — test data, not security
    distinct: list[dict[str, Any]] = []
    out: list[AlertEvent] = []
    for i in range(ALERTS):
        if distinct and rnd.random() < DUPLICATE_SHARE:
            base = rnd.choice(distinct)
        else:
            svc = rnd.randrange(SERVICES)
            base = {
                "resource": f"svc-{svc:02d}-api",
                "alert_type": rnd.choice(["error_rate", "latency", "saturation"]),
                # Words, not numbers: normalize() folds digits, and the soak
                # wants hundreds of genuinely distinct signatures.
                "error_signature": f"sig:{_word(svc)}:{_word(len(distinct))}",
            }
            distinct.append(base)
        out.append(
            AlertEvent(
                tenant_id="acme",
                source="servicenow",
                origin="alert",
                external_id=f"INC{i:07d}",
                table="incident",
                ticket_number=None,
                received_at=T0 + timedelta(seconds=WINDOW_S * i / ALERTS),
                subject=f"{base['resource']} {base['alert_type']}",
                body="storm",
                severity_hint=Severity.P3,
                cmdb_ci=None,
                raw_ref=f"soak:{i}",
                **base,
            )
        )
    return out


class _ChaosSink:
    """``sn_500`` on every 7th call; ``crash_after_send`` on the first 3 sends."""

    def __init__(self, inner: FakeServiceNow) -> None:
        self.inner = inner
        self.calls = 0
        self.crashes = 0

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.calls % FAIL_EVERY == 0:
            raise ServiceNowError("injected fault: sn_500", retryable=True, status=500)  # noqa: TRY003

    def append_journal(
        self, table: RecordType, external_id: str, field: NoteVisibility, text: str
    ) -> ExternalRef:
        self._maybe_fail()
        ref = self.inner.append_journal(table, external_id, field, text)
        if self.crashes < CRASHES:
            self.crashes += 1
            raise SimulatedCrash("injected fault: crash_after_send")  # noqa: TRY003
        return ref

    def update_fields(self, table: RecordType, external_id: str, fields: Any) -> ExternalRef:
        self._maybe_fail()
        return self.inner.update_fields(table, external_id, fields)

    def find_marker(self, table: RecordType, external_id: str, marker: str) -> ExternalRef | None:
        self._maybe_fail()
        return self.inner.find_marker(table, external_id, marker)


@dataclass(frozen=True)
class StormRun:
    db: Engine
    alerts: list[AlertEvent]
    llm_calls: int
    sn: FakeServiceNow
    seconds: float
    crashes: int


def _seed_grants(db: Engine) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                "VALUES ('sub-ada', 'acme', 'ada@x', 'Ada', 1, 0)"
            )
        )
        for i in range(SERVICES):
            conn.execute(
                text(
                    "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                    "requested_by, requested_at_ts, granted_by, granted_at_ts, valid_to_ts) "
                    "VALUES (:id, 'acme', 'work_note', :scope, 'approved', 'config:soak', 1, "
                    "'sub-ada', 1, 9999999999)"
                ),
                {"id": f"grnt_{i:02d}", "scope": f"service:svc-{i:02d}:work_note"},
            )


@pytest.fixture(scope="module")
def storm(_db_engine: Engine) -> StormRun:
    db = _db_engine
    _reset_schema(db)
    _seed_grants(db)
    clock = FrozenClock(start=T0)
    ids = SequentialIdGenerator()
    uow = SqlUnitOfWork(db)
    aead = Aead(master_key=b"test" * 8, keys=SqlKeyStore(uow), clock=clock)
    events = SqlEventSink(uow=uow, cipher=aead)
    catalog = _catalog()
    fake_llm = FakeLLMProvider()
    fake_llm.respond(_llm_json())
    llm = LLMClient(
        provider=fake_llm,
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=PromptRegistry(root=_REPO / "prompts"),
        events=events,
        ids=ids,
        clock=clock,
        slots={SlotName.CLASSIFY: SlotSpec(model_id=_MODEL, timeout_s=15.0, max_output_tokens=400)},
    )
    writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog,
        ttl=timedelta(hours=24),
        outbox=writer,
    )
    triage = TriageService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=CaseRepo(uow),
        classifier=Classifier(
            rule_set=RuleSet(rules=[]),
            llm=llm,
            calibration=_table(),
            min_score=0.80,
            routing=_routing(),
        ),
        gate=gate,
        catalog=catalog,
        grouping=GroupingService(uow=uow, ids=ids, catalog=catalog, tenant_id="acme"),
        spend=SpendBreaker(uow=uow, daily_budget_tokens=50_000_000),
    )
    sn = FakeServiceNow()
    chaos = _ChaosSink(sn)
    sink: TicketSink = chaos
    executor = OutboxExecutor(
        uow=uow,
        repo=OutboxRepo(uow),
        clock=clock,
        ids=ids,
        events=events,
        sink=sink,
        handlers=HANDLERS,
    )
    alerts = _alerts()
    started = time.monotonic()
    for evt in alerts:
        clock.set(evt.received_at)
        triage.handle(evt)
    # Drain the outbox through the chaos; crashed claims are reclaimed after the lease.
    for _ in range(20 * ALERTS):
        try:
            result = executor.run_once(clock.now())
        except SimulatedCrash:
            continue
        if result.status == "none":
            clock.advance(DEFAULT_LEASE_S + 30)  # retries come due; dead claims expire
            if executor.run_once(clock.now()).status == "none":
                break
    return StormRun(
        db=db,
        alerts=alerts,
        llm_calls=len(fake_llm.captured),
        sn=sn,
        seconds=time.monotonic() - started,
        crashes=chaos.crashes,
    )


def _scalar(db: Engine, sql: str) -> Any:
    with db.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


def test_storm_zero_duplicate_cases(storm: StormRun) -> None:
    # Distinct by the domain's own identity: the normalized fingerprint (01 §7.3).
    distinct = {fingerprint(a) for a in storm.alerts}
    assert _scalar(storm.db, "SELECT COUNT(*) FROM cases") == len(distinct)
    assert _scalar(storm.db, "SELECT COUNT(DISTINCT fingerprint) FROM cases") == len(distinct)
    duplicates = _scalar(storm.db, "SELECT COALESCE(SUM(duplicate_count), 0) FROM cases")
    assert duplicates == len(storm.alerts) - len(distinct), "every repeat linked, none dropped"


def test_storm_llm_calls_bounded_by_groups(storm: StormRun) -> None:
    groups = _scalar(storm.db, "SELECT COUNT(*) FROM case_groups")
    ungrouped = _scalar(storm.db, "SELECT COUNT(*) FROM cases WHERE group_id IS NULL")
    assert groups >= 1, "the storm opened at least one group"
    # Before storm mode (> 20 new cases in 5 min) cases are triaged one by one;
    # after it, one classification per group.
    headroom = STORM_CASES + 1
    assert storm.llm_calls <= groups + headroom, (storm.llm_calls, groups, ungrouped)
    members = _scalar(
        storm.db, "SELECT COUNT(*) FROM cases WHERE decision_json LIKE '%storm-member%'"
    )
    assert members > 0.5 * _scalar(storm.db, "SELECT COUNT(*) FROM cases")


def test_storm_zero_double_sends(storm: StormRun) -> None:
    rows = _scalar(storm.db, "SELECT COUNT(*) FROM outbox")
    done = _scalar(storm.db, "SELECT COUNT(*) FROM outbox WHERE status = 'done'")
    assert rows > 0 and done == rows, "every queued note was delivered despite the faults"
    assert storm.crashes == CRASHES, "the crash-after-send window was exercised"
    markers: list[str] = []
    for (external_id,) in _rows(
        storm.db, "SELECT c.external_id FROM outbox o JOIN cases c ON c.id = o.case_id"
    ):
        for entry in storm.sn.journal_entries(
            RecordType.INCIDENT, external_id, NoteVisibility.INTERNAL
        ):
            markers.append(entry.splitlines()[-1])
    assert len(markers) == rows, "one work note per outbox row: 0 double-sends"
    assert len(set(markers)) == rows


def _rows(db: Engine, sql: str) -> list[Any]:
    with db.connect() as conn:
        return list(conn.execute(text(sql)).all())


def test_storm_completes_within_budget(storm: StormRun) -> None:
    assert storm.seconds < 15 * 60, "04 §1: the soak runs in under 15 minutes"


def test_storm_report(storm: StormRun, capsys: pytest.CaptureFixture[str]) -> None:
    """The soak's numbers, printed with ``-s`` (and sanity-checked)."""
    report = {
        "alerts": len(storm.alerts),
        "cases": _scalar(storm.db, "SELECT COUNT(*) FROM cases"),
        "groups": _scalar(storm.db, "SELECT COUNT(*) FROM case_groups"),
        "storm_members": _scalar(
            storm.db, "SELECT COUNT(*) FROM cases WHERE decision_json LIKE '%storm-member%'"
        ),
        "llm_calls": storm.llm_calls,
        "outbox_rows": _scalar(storm.db, "SELECT COUNT(*) FROM outbox"),
        "crashes": storm.crashes,
        "seconds": round(storm.seconds, 2),
    }
    with capsys.disabled():
        print(f"\nstorm soak: {report}")
    assert report["cases"] > 500, "the generator produced a real storm, not a handful"
