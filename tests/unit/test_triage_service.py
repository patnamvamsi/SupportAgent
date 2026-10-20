"""WP-011 Loop A pipeline + propose: TriageService (10-wp §WP-011).

Eight canonical test IDs (10-wp §WP-011):

- ``test_every_action_traced``
- ``test_triage_duplicate_returns_linked``
- ``test_triage_escalation_retriages_in_place``
- ``test_triage_routed_submits_proposals``
- ``test_triage_safe_default_proposes_nothing``
- ``test_triage_work_note_auto_approved_by_setup_grant``
- ``test_triage_shadow_mode_has_no_sink``
- ``test_fault_injection_suite``

The TriageService.handle() path is verified against a real SQLite DB, with
the real LLMClient (over FakeLLMProvider), Classifier, GateService, and
SqlEventSink. The container test checks that shadow mode does not
construct a TicketSink.
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
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNow
from supportagent.adapters.llm.client import LLMClient, SlotSpec
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.faults import FaultInjectingLLMProvider
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.app.classify import Classifier
from supportagent.app.gate import GateService
from supportagent.app.handlers import HANDLERS
from supportagent.app.outbox import Authorization, OutboxExecutor
from supportagent.app.replay import ReplayService
from supportagent.app.triage import TriageService
from supportagent.bootstrap import build_container
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ServiceNowError
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
from supportagent.domain.model.triage import (
    Linked,
    Retriaged,
    SafeDefaulted,
    Triaged,
)
from supportagent.domain.preclass import RuleSet
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.llm import LLMProvider
from tests.builders import an_alert

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5-20250929"
_LABEL = "payments/api"
_STRONG = CalibrationCountRow(n=40, correct=40)
_TTL = timedelta(hours=24)
# ADR-0005: T1 kinds are scoped to the catalog service, not the record.
_SCOPE = "service:payments:work_note"


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


def _llm_json(classification: str = _LABEL, severity: str = "P3") -> str:
    return (
        '{"classification": "' + classification + '", "severity": "' + severity + '",'
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
    # ``aead`` (the fixture) is built over the SAME ``uow`` instance, so its
    # internal ``SqlKeyStore`` and this sink's writes share one active
    # connection inside ``TriageService``'s ``with uow:`` block.
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


def _seed_grant(db: Any, *, scope: str = _SCOPE, grant_id: str = "grnt_seed") -> None:
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


def test_every_action_traced(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """Every emitted action (proposal) carries the trace; replay shows the path."""
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, Triaged)
    # Every action (proposal) carries the trace id.
    assert outcome.proposals
    assert all(p.trace_id for p in outcome.proposals)
    trace_id = outcome.proposals[0].trace_id
    # Replay shows the full path.
    with w.uow:
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


def test_triage_duplicate_returns_linked(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """A duplicate alert (same fingerprint, no escalation) returns Linked."""
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    first = w.triage.handle(_evt())
    assert isinstance(first, Triaged)
    # Same fingerprint, same severity hint → duplicate, no escalation.
    second = w.triage.handle(_evt())
    assert isinstance(second, Linked)
    assert second.case_id == first.case_id


def test_triage_escalation_retriages_in_place(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """An escalation (same fingerprint, higher severity) re-triages in place."""
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    # First alert: P3 hint (lower severity).
    first = w.triage.handle(_evt(severity_hint=Severity.P3))
    assert isinstance(first, Triaged)
    # Second alert: same fingerprint, P1 hint (escalation).
    second = w.triage.handle(_evt(severity_hint=Severity.P1))
    assert isinstance(second, Retriaged)
    assert second.case_id == first.case_id


def test_triage_routed_submits_proposals(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """A routed case submits proposals through the GateService."""
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, Triaged)
    assert outcome.proposals
    # Each proposal is a WORK_NOTE with the right scope.
    for p in outcome.proposals:
        assert p.action_kind == ActionKind.WORK_NOTE
        assert p.scope == _SCOPE


def test_triage_safe_default_proposes_nothing(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """A safe-default case (unresolved service) proposes nothing."""
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    # Resource not in the catalog → unresolved-service → safe default.
    outcome = w.triage.handle(_evt(resource="unknown-service"))
    assert isinstance(outcome, SafeDefaulted)
    assert outcome.decision.reason == "unresolved-service"


def test_triage_work_note_auto_approved_by_setup_grant(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    """A setup grant auto-approves the work_note proposal."""
    fake_llm.respond(_llm_json())
    _seed_grant(db, scope=_SCOPE)
    w = _wire(uow, clock, ids, aead, fake_llm)
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, Triaged)
    assert outcome.proposals
    # The proposal is auto-approved by the setup grant.
    for p in outcome.proposals:
        assert p.status == ProposalStatus.APPROVED


def _shadow_config() -> Any:
    """A resolved shadow-mode AppConfig (02 §1: v0.0 accepts only 'shadow')."""
    resolved: dict[str, object] = {
        "config_version": 1,
        "tenant": {"id": "acme", "display_name": "Acme Corp"},
        "mode": "shadow",
        "database": {"url": "sqlite:///:memory:"},
        "servicenow": {
            "instance_url": "https://acme.service-now.com",
            "auth": {
                "kind": "oauth_client_credentials",
                "client_id": "sn-client-id",
                "client_secret": "sn-client-secret",
            },
            "integration_user": "svc_supportagent",
            "tables": ["incident"],
            "poll_interval_s": 60,
            "overlap_s": 120,
            "page_size": 200,
            "qps_limit": 5,
        },
        "identity": {
            "oidc_issuer": "https://login.microsoftonline.com/acme-tenant-id/v2.0",
            "client_id": "oidc-client-id",
            "client_secret": "oidc-client-secret",
            "redirect_url": "https://supportagent.acme.internal/auth/callback",
            "session_secret": "test" * 8,
        },
        "llm": {
            "provider": "fixture",
            "api_key": None,
            "base_url": None,
            "slots": {
                "classify": {
                    "model_id": "claude-sonnet-4-5-20250929",
                    "timeout_s": 15,
                    "max_output_tokens": 400,
                }
            },
            "daily_budget_tokens": 2_000_000,
        },
        "redaction": {"custom_patterns": [], "tenant_constants": []},
        "calibration": {
            "path": "evals/artifacts/calibration.json",
            "min_grade": "smoke",
            "min_score": 0.80,
        },
        "paths": {
            "taxonomy": "taxonomy/classification.yaml",
            "hypotheses": "taxonomy/hypotheses.yaml",
            "catalog": "catalog/services.yaml",
            "prompts": "prompts/",
            "runbooks": "runbooks/",
        },
        "routing": [
            {
                "id": "db-saturation",
                "match": {"classification": "database/saturation", "severity": ["P1", "P2", "P3"]},
                "actions": ["work_note"],
                "rca": "none",
                "comms": None,
            }
        ],
        "default": {"id": "generic", "actions": ["work_note"], "rca": "none"},
        "proposals": {"ttl_hours": 24},
        "grants_request": [],
        "retention": {"events_days": 395, "comms_days": 395, "alerts_days": 90},
    }
    return AppConfig.model_validate(resolved)


def test_triage_shadow_mode_has_no_sink() -> None:
    """Shadow mode does not construct a TicketSink."""
    config = _shadow_config()
    assert config.mode == "shadow"
    container = build_container(config)
    assert "TicketSink" not in container, "shadow mode must not construct a TicketSink"


def test_fault_injection_suite(  # noqa: PLR0913, PLR0917
    db: Any,
    uow: Any,
    clock: Any,
    ids: Any,
    aead: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.0 fault subset: llm_timeout, llm_garbage → safe default; sn_500 → retry."""
    # --- llm_timeout → SafeDefaulted (reason llm-failure) -----------------
    monkeypatch.setenv("SUPPORTAGENT_FAULT_INJECT", "llm_timeout")
    w = _wire(uow, clock, ids, aead, FaultInjectingLLMProvider(FakeLLMProvider()))
    outcome = w.triage.handle(_evt(error_signature="5xx:timeout"))
    assert isinstance(outcome, SafeDefaulted)
    assert outcome.decision.reason == "llm-failure"

    # --- llm_garbage → SafeDefaulted (reason llm-failure) -----------------
    monkeypatch.setenv("SUPPORTAGENT_FAULT_INJECT", "llm_garbage")
    w2 = _wire(uow, clock, ids, aead, FaultInjectingLLMProvider(FakeLLMProvider()))
    outcome2 = w2.triage.handle(_evt(error_signature="5xx:garbage"))
    assert isinstance(outcome2, SafeDefaulted)
    assert outcome2.decision.reason == "llm-failure"
    monkeypatch.delenv("SUPPORTAGENT_FAULT_INJECT", raising=False)

    # --- sn_500 → outbox retry --------------------------------------------
    # The triage flow ends at gate.submit; the SN sink is called during
    # outbox execution. Queue a 500 fault and verify the retry path.
    # Seed a case with external_table + external_id (required by _claim). Its id
    # must not collide with the llm-failure cases above, which are persisted.
    with db.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO cases "
            "(id, tenant_id, fingerprint, origin, status, trace_id, duplicate_count, "
            "opened_ts, last_alert_ts, external_table, external_id, "
            "service, severity, classification) "
            "VALUES ('case_sn500', 'acme', 'fp_1', 'alert', 'open', 'trace_0001', 0, "
            "0, 0, 'incident', 'INC0000001', 'payments', 'P3', 'payments/api')"
        )
    _seed_grant(db)  # outbox.policy_grant_id is an FK to policy_grants.id

    sn = FakeServiceNow()
    sn.queue_fault(ServiceNowError("SN 500", retryable=True))
    # ``aead`` (the fixture) is built over this same ``uow`` instance, so the
    # sink's writes and the key-store lookup share one active connection
    # inside ``OutboxExecutor``'s ``with uow:`` block.
    events3 = SqlEventSink(uow=uow, cipher=aead)

    outbox_repo = OutboxRepo(uow)
    spec = ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(service="payments", system="servicenow", ref="INC0000001"),
        scope=_SCOPE,
        payload={"template_id": "work_note_v1", "text": "work note"},
    )
    auth = Authorization(policy_grant_id="grnt_seed")
    executor = OutboxExecutor(
        uow=uow,
        repo=outbox_repo,
        clock=clock,
        ids=ids,
        events=events3,
        sink=sn,
        handlers=HANDLERS,
    )
    trace = TraceContext.new("acme", "trace_0001", "span_obx")
    executor.enqueue(auth, spec, "case_sn500", trace)
    result = executor.run_once(clock.now())
    assert result.status == "retried"
