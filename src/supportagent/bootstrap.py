"""Composition root — the only place adapters are constructed (01 §2).

``build_container(config)`` wires ports to adapters and hands the graph to
``entrypoints``. ``run_migrations()`` is the programmatic home of
``supportagent migrate`` (02 §4): it points Alembic at the checked-in
migration history and the resolved database URL, then upgrades — or, with
``allow_downgrade``, downgrades (03 §4: downgrades are refused without the
flag; the CLI maps that to exit 2). ``replay_trace()`` is the home of
``supportagent replay`` (02 §4): it wires the event read path (DB + crypto +
app service) and returns the ``ReplayView``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import anthropic
import httpx
import openai
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from supportagent.adapters.cel.check import CelCheckCompiler
from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.engine import make_engine
from supportagent.adapters.db.probe import SqlSchemaProbe
from supportagent.adapters.db.repos.automation import SqlAutomationActors
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.leases import SqlLease
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.repos.retention import SqlRetention
from supportagent.adapters.db.repos.shadow import ShadowRepo
from supportagent.adapters.db.repos.sync import SyncRepo
from supportagent.adapters.db.repos.webhooks import SqlNonceStore, WebhookInbox
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.faults import FaultInjectingTicketSink, FaultInjectingTool
from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.comms_loader import CommsCatalog, load_comms_catalog
from supportagent.adapters.files.deprecation_loader import load_deprecation_feed
from supportagent.adapters.files.eval_loader import load_eval_documents, load_yaml_document
from supportagent.adapters.files.gates_loader import load_promotion_gate, load_quality_gates
from supportagent.adapters.files.preclass_loader import load_preclass_rules
from supportagent.adapters.files.runbook_loader import load_probes, load_runbooks
from supportagent.adapters.files.taxonomy_loader import load_classification, load_hypotheses
from supportagent.adapters.identity.oidc import OIDCClient
from supportagent.adapters.llm.client import (
    CachingLLMProvider,
    FaultInjectingLLMProvider,
    FixtureAnswer,
    FixtureLLMProvider,
    LLMClient,
    RetiredModelGuard,
    SlotSpec,
    build_llm_client,
)
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.adapters.paging.noop import RoutedPaging
from supportagent.adapters.paging.opsgenie import OpsgeniePaging
from supportagent.adapters.paging.pagerduty import PagerDutyPaging
from supportagent.adapters.resilience.breaker import (
    CircuitBreaker,
    CircuitBreakerLLMProvider,
    CircuitBreakerTicketSink,
)
from supportagent.adapters.resilience.retry import RetryingTicketSink
from supportagent.adapters.servicenow.auth import (
    BasicAuth,
    OAuthClientCredentials,
    ServiceNowAuth,
)
from supportagent.adapters.servicenow.export import read_export
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.mapping import record_to_alert
from supportagent.adapters.servicenow.probe import ServiceNowTableProbe
from supportagent.adapters.servicenow.sink import ServiceNowSink
from supportagent.adapters.servicenow.source import ServiceNowSource
from supportagent.adapters.system.clock import ReplayClock, SystemClock
from supportagent.adapters.system.ids import UlidIdGenerator
from supportagent.adapters.tools import airflow as airflow_tool
from supportagent.adapters.tools import cron as cron_tool
from supportagent.adapters.tools import gha as gha_tool
from supportagent.adapters.tools import logs as logs_tool
from supportagent.adapters.tools import oncall as oncall_tool
from supportagent.adapters.tools.airflow import AirflowTool
from supportagent.adapters.tools.cron import CronStatusTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.logs import LogsTool
from supportagent.adapters.tools.oncall import OnCallTool
from supportagent.app.approvals import ApprovalService
from supportagent.app.classify import Classifier
from supportagent.app.comms import CadenceJob, CommsPlanner, CommsPlans
from supportagent.app.doctor import Bundle, Doctor, DoctorContext, DoctorReport, default_checks
from supportagent.app.feedback import FeedbackService
from supportagent.app.gate import GateService
from supportagent.app.grouping import GroupingService
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.memory import CaseMemory
from supportagent.app.outbox import OutboxWriter
from supportagent.app.queries import AdminQueries
from supportagent.app.rca import BufferedEvents, Knowledge, RcaJob, RcaService, lite_route_ids
from supportagent.app.replay import ReplayService, ReplayView
from supportagent.app.retention import RetentionService
from supportagent.app.setup import SetupService, expand_grant_requests
from supportagent.app.shadow import ShadowInbound, ShadowRunner, import_history
from supportagent.app.spend import SpendBreaker
from supportagent.app.stats import StatsService
from supportagent.app.sync import SyncService
from supportagent.app.tools import ToolRegistry
from supportagent.app.triage import TriageService
from supportagent.app.webhooks import WebhookDrainer, WebhookService, WebhookSource
from supportagent.config.loader import (
    load_config_default,
    load_config_offline,
    load_fault_injection_default,
    load_master_key_default,
)
from supportagent.config.schema import AppConfig, RoutingRow
from supportagent.config.schema import BasicAuth as BasicAuthConfig
from supportagent.domain.deprecation import deprecation_notices
from supportagent.domain.errors import ConfigError, DomainValidationError, ExternalServiceError
from supportagent.domain.faults import LLM_FAULTS, SINK_FAULTS, Fault
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.preclass import RuleSet
from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.model.shadow import ShadowRecord
from supportagent.domain.model.triage import TriageDecision
from supportagent.domain.normalize import normalize
from supportagent.domain.promotion import PromotionGate
from supportagent.domain.quality import DEFAULT_GATES, QualityGate, SplitCase, split_violations
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.worker.jobs import (
    GROUPS_CLOSE_INTERVAL_S,
    GROUPS_CLOSE_LEASE,
    LLM_DEPRECATION_INTERVAL_S,
    LLM_DEPRECATION_LEASE,
    PROPOSALS_EXPIRE_INTERVAL_S,
    PROPOSALS_EXPIRE_LEASE,
    RCA_RUN_INTERVAL_S,
    RCA_RUN_LEASE,
    RETENTION_SWEEP_INTERVAL_S,
    RETENTION_SWEEP_LEASE,
    WEBHOOKS_DRAIN_INTERVAL_S,
    WEBHOOKS_DRAIN_LEASE,
    JobResult,
    groups_close,
    llm_deprecation_check,
    proposals_expire,
    rca_run,
    retention_sweep,
    sync_poll,
    sync_poll_lease,
    webhooks_drain,
)
from supportagent.entrypoints.worker.loop import ScheduledJob, Worker
from supportagent.evals.cases import EvalCase, FixtureSpec, parse_cases
from supportagent.evals.harness import Manifest
from supportagent.evals.quality import RecallReport
from supportagent.evals.scoring import CaseRun
from supportagent.ports.clock import Clock
from supportagent.ports.diagnostics import AutomationActorStore, DiscoveryProbe
from supportagent.ports.events import EventSink
from supportagent.ports.identity import IdentityProvider
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse
from supportagent.ports.tickets import SnTable, TicketSink, TicketSource
from supportagent.ports.tools import Tool

# ``src/supportagent/bootstrap.py`` → repo root (the checkout the user runs).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_uow(config: AppConfig) -> SqlUnitOfWork:
    """A fresh unit of work on the configured engine (call-time, lazy)."""
    return SqlUnitOfWork(make_engine(config.database.url.get_secret_value()))


def _build_catalog(config: AppConfig) -> Catalog:
    """The service catalog from ``paths.catalog`` (call-time file I/O)."""
    return load_catalog(Path(config.paths.catalog))


def _route_for_row(row: RoutingRow) -> RoutingRule:
    """One routing rule from a config row; the match clause is required (02 §1)."""
    match = row.match
    if match is None:
        raise ConfigError(  # noqa: TRY003 — the config gate enforces a match clause
            "a routing row must carry a match clause (02 §1)"
        )
    return RoutingRule(
        match_classification=match.classification,
        severities=match.severity,
        route=Route(id=row.id, actions=row.actions, rca=row.rca, comms=row.comms),
    )


def _build_routing(config: AppConfig) -> RoutingTable:
    """The routing table from the ``routing`` + ``default`` rows (02 §1)."""
    default = config.default
    if default is None:
        raise ConfigError(  # noqa: TRY003 — a routing table always has a fallback
            "config.default is required for the routing table (02 §1)"
        )
    return RoutingTable(
        rules=[_route_for_row(row) for row in config.routing],
        default=Route(id=default.id, actions=default.actions, rca=default.rca, comms=default.comms),
    )


def _build_calibration(config: AppConfig) -> CalibrationTable:
    """The calibration artifact, checked against the configured model (ADR-0002)."""
    return load_calibration(
        Path(config.calibration.path),
        configured_model_id=config.llm.slots[SlotName.CLASSIFY.value].model_id,
        min_grade=config.calibration.min_grade,
    )


_log = logging.getLogger("supportagent.bootstrap")

#: ``snapshot_id`` of the empty table that replaces a rejected one (BL-018).
REJECTED_CALIBRATION = "rejected-calibration"


def _classifier_calibration(config: AppConfig) -> CalibrationTable:
    """BL-018 / 01 §7.7: a rejected calibration table means every label scores 0.

    Startup doesn't fail: the worker keeps polling and every case safe-defaults
    (``low-calibrated-score``, the human queue, INV-05). The rejection is
    logged as an error, and ``doctor``'s calibration check fails with the
    reason, since it loads through the strict ``_build_calibration``.
    """
    try:
        return _build_calibration(config)
    except ConfigError as exc:
        _log.error("calibration rejected; every label scores 0 until fixed: %s", exc)
        return CalibrationTable(
            grade="smoke",
            snapshot_id=REJECTED_CALIBRATION,
            model_id=config.llm.slots[SlotName.CLASSIFY.value].model_id,
            labels={},
        )


def _build_ruleset(config: AppConfig) -> RuleSet:
    """The pre-classifier rules beside the taxonomy (ARCH §4.2, 10-wp WP-010).

    An empty rule set would make every case abstain, so the cross-check never
    runs and every P1/P2 is capped — the shipped rules are always loaded.
    """
    return load_preclass_rules(Path(config.paths.taxonomy).parent / "preclass_rules.yaml")


def _classify_slots(config: AppConfig) -> dict[SlotName, SlotSpec]:
    """Every configured slot the system knows (``classify`` is required; v0.1 adds
    ``propose_hypotheses`` and ``entail``). An unconfigured slot fails closed."""
    known = {slot.value for slot in SlotName}
    return {
        SlotName(name): SlotSpec(
            model_id=slot.model_id,
            timeout_s=slot.timeout_s,
            max_output_tokens=slot.max_output_tokens,
        )
        for name, slot in config.llm.slots.items()
        if name in known
    }


def _build_llm(  # noqa: PLR0913 — the client's ports plus the two seams
    config: AppConfig,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
    provider_override: LLMProvider | None = None,
    *,
    decorate: Callable[[LLMProvider], LLMProvider] | None = None,
    resilient: bool = True,
) -> LLMClient:
    """The redacting LLM client for the configured provider (01 §5, INV-06).

    ``provider_override`` is the test seam (a scripted provider); it is still
    wrapped in ``LLMClient``, so redaction and the ``llm`` event apply.
    ``decorate`` wraps the provider inside the client (the eval harness
    records the redacted requests with it). ``resilient`` adds the WP-106
    chain around it (``_resilience``); the eval harness turns it off.
    """
    chain = _compose(_resilience(config, clock) if resilient else None, decorate)
    if provider_override is not None:
        return LLMClient(
            provider=chain(provider_override) if chain else provider_override,
            redactor=Redactor(detectors=built_in_detectors()),
            prompts=PromptRegistry(root=Path(config.paths.prompts)),
            events=events,
            ids=ids,
            clock=clock,
            slots=_classify_slots(config),
        )
    provider = config.llm.provider
    api_key = config.llm.api_key
    sdk_client: anthropic.Anthropic | openai.OpenAI
    if provider == "anthropic":
        if api_key is None:
            raise ConfigError("llm.api_key is required for provider 'anthropic' (02 §1)")  # noqa: TRY003
        sdk_client = anthropic.Anthropic(api_key=api_key.get_secret_value())
    elif provider == "openai":
        if api_key is None:
            raise ConfigError("llm.api_key is required for provider 'openai' (02 §1)")  # noqa: TRY003
        sdk_client = openai.OpenAI(api_key=api_key.get_secret_value())
    else:
        raise ConfigError(  # noqa: TRY003 — the v0.0 container wires only the two cloud providers
            f"container LLM wiring supports anthropic/openai; got '{provider}' (01 §5)"
        )
    return build_llm_client(
        provider_name=provider,
        sdk_client=sdk_client,
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=PromptRegistry(root=Path(config.paths.prompts)),
        events=events,
        ids=ids,
        clock=clock,
        slots=_classify_slots(config),
        decorate=chain,
    )


def _compose(
    outer: Callable[[LLMProvider], LLMProvider] | None,
    inner: Callable[[LLMProvider], LLMProvider] | None,
) -> Callable[[LLMProvider], LLMProvider] | None:
    """``outer(inner(provider))``; either side may be absent."""
    if outer is None:
        return inner
    if inner is None:
        return outer
    return lambda provider: outer(inner(provider))


def _resilience(config: AppConfig, clock: Clock) -> Callable[[LLMProvider], LLMProvider]:
    """11-wp WP-106, outermost first: retired-model guard → breaker → faults → provider.

    Injected faults sit inside the breaker, so an injected outage trips it
    like a real one. The retired guard sits outside, so a retired model
    never counts as the provider being down.
    """
    faults = {f.value for f in load_fault_injection_default() & LLM_FAULTS}
    feed = (
        load_deprecation_feed(Path(config.llm.deprecation_feed))
        if (config.llm.deprecation_feed)
        else {}
    )
    breaker = CircuitBreaker(clock=clock, name="llm")

    def wrap(provider: LLMProvider) -> LLMProvider:
        inner: LLMProvider = FaultInjectingLLMProvider(provider, faults) if faults else provider
        guarded: LLMProvider = CircuitBreakerLLMProvider(inner, breaker)
        return RetiredModelGuard(guarded, feed=feed, clock=clock) if feed else guarded

    return wrap


def build_ticket_sink(
    config: AppConfig, *, clock: Clock, transport: httpx.BaseTransport | None = None
) -> TicketSink:
    """The write side of ServiceNow (assisted mode; shadow never builds it).

    breaker → retry → faults → ``ServiceNowSink`` (11-wp WP-106). The retry
    never repeats an ambiguous journal append (``RetryingTicketSink``).
    """
    sink: TicketSink = ServiceNowSink(
        http=_servicenow_http(config, clock=clock, transport=transport, max_tries=1)
    )
    faults = load_fault_injection_default() & SINK_FAULTS
    if faults:
        sink = FaultInjectingTicketSink(sink, faults)
    return CircuitBreakerTicketSink(
        RetryingTicketSink(sink), CircuitBreaker(clock=clock, name="servicenow")
    )


#: Seconds of headroom between the worst-case claim and its lease (BL-001).
_LEASE_MARGIN_S = 60
#: ``update_fields`` reads the record, then patches it: two requests per try.
_UPDATE_REQUESTS = 2


def outbox_claim_budget_s(config: AppConfig, *, clock: Clock) -> float:
    """BL-001: the longest one outbox claim can hold its row, for ``build_ticket_sink``.

    One claim reconciles (``find_marker``) and then sends (``append_journal``
    or ``update_fields``), each through ``RetryingTicketSink`` over a
    single-attempt HTTP client. Every try timing out and every wait maxed out
    gives the bound.
    """
    http = _servicenow_http(config, clock=clock, max_tries=1).worst_case_s
    retry = RetryingTicketSink(ServiceNowSink(http=_servicenow_http(config, clock=clock)))
    reconcile = retry.worst_case_s(http)
    send = max(retry.worst_case_s(http), retry.worst_case_s(_UPDATE_REQUESTS * http))
    return reconcile + send


def outbox_claim_lease_s(config: AppConfig, *, clock: Clock) -> int:
    """The claim lease the assisted-mode executor uses: the budget plus a margin."""
    return math.ceil(outbox_claim_budget_s(config, clock=clock)) + _LEASE_MARGIN_S


def _build_classifier(
    config: AppConfig,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
    provider_override: LLMProvider | None = None,
) -> Classifier:
    """The ARCH §4 classifier: shipped pre-classifier rules, LLM, calibration, routing."""
    return Classifier(
        rule_set=_build_ruleset(config),
        llm=_build_llm(config, events, ids, clock, provider_override),
        calibration=_classifier_calibration(config),
        min_score=config.calibration.min_score,
        routing=_build_routing(config),
    )


def _build_triage(config: AppConfig) -> TriageService:
    """The live triage graph on one shared unit of work (10-wp WP-011; assisted mode)."""
    uow = _build_uow(config)
    clock = SystemClock()
    ids = UlidIdGenerator()
    aead = Aead(master_key=load_master_key_default(), keys=SqlKeyStore(uow), clock=clock)
    events = SqlEventSink(uow=uow, cipher=aead)
    catalog = _build_catalog(config)
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog,
        ttl=timedelta(hours=config.proposals.ttl_hours),
        # BL-024: grant-authorized proposals enqueue in the triage UoW.
        outbox=OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids),
    )
    return TriageService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=CaseRepo(uow),
        classifier=_build_classifier(config, events, ids, clock),
        gate=gate,
        catalog=catalog,
        tenant_constants=config.redaction.tenant_constants,
        planner=CommsPlanner(
            uow=uow, ids=ids, gate=gate, plans=_comms(config).plans, tenant_id=config.tenant.id
        ),
        grouping=GroupingService(uow=uow, ids=ids, catalog=catalog, tenant_id=config.tenant.id),
        spend=SpendBreaker(uow=uow, daily_budget_tokens=config.llm.daily_budget_tokens),
        memory=_build_memory(config, uow, clock),
        paging=build_paging(config),
    )


def build_paging(
    config: AppConfig, *, transport: httpx.BaseTransport | None = None
) -> RoutedPaging | None:
    """WP-112: the configured paging providers (attach-only), or ``None`` when there are none.

    ``None`` means the Null Object: no page lookups, no ``page_recommended``,
    no ``enrich_page`` handler; ``doctor``'s paging check warns.
    """
    cfg = config.paging
    providers: list[PagerDutyPaging | OpsgeniePaging] = []
    if cfg.pagerduty is not None:
        providers.append(
            PagerDutyPaging(
                client=httpx.Client(
                    base_url=cfg.pagerduty.base_url,
                    headers={
                        "Authorization": f"Token token={cfg.pagerduty.token.get_secret_value()}",
                        "Accept": "application/vnd.pagerduty+json;version=2",
                        "From": cfg.pagerduty.from_email,
                    },
                    transport=transport,
                    timeout=10.0,
                )
            )
        )
    if cfg.opsgenie is not None:
        providers.append(
            OpsgeniePaging(
                client=httpx.Client(
                    base_url=cfg.opsgenie.base_url,
                    headers={
                        "Authorization": f"GenieKey {cfg.opsgenie.api_key.get_secret_value()}"
                    },
                    transport=transport,
                    timeout=10.0,
                )
            )
        )
    return RoutedPaging(providers) if providers else None


def _build_memory(config: AppConfig, uow: SqlUnitOfWork, clock: Clock) -> CaseMemory:
    """Case memory (WP-108). Text is redacted with the LLM boundary's detectors."""
    redactor = Redactor(detectors=built_in_detectors())
    return CaseMemory(
        uow=uow,
        clock=clock,
        tenant_id=config.tenant.id,
        redact=lambda text: redactor.redact(text).text,
        tenant_constants=config.redaction.tenant_constants,
        weights=config.memory.weights,
    )


def _comms(config: AppConfig) -> CommsCatalog:
    """The comms plan templates; every routing row's ``comms`` must name one (WP-102)."""
    catalog = load_comms_catalog(Path(config.paths.comms))
    rows = [*config.routing, *([config.default] if config.default else [])]
    unknown = sorted({r.comms for r in rows if r.comms and r.comms not in catalog.plans})
    if unknown:
        raise ConfigError(  # noqa: TRY003
            f"routing names unknown comms plan template(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(catalog.plans)) or 'none'})"
        )
    return catalog


def build_cadence_job(  # noqa: PLR0913 — config plus the injectable ports
    config: AppConfig,
    *,
    engine: Engine,
    source: TicketSource,
    clock: Clock,
    ids: IdGenerator,
    master_key: bytes | None = None,
) -> CadenceJob:
    """``comms.cadence`` (02 §7). Assisted mode: shadow has no outbox writer to feed."""
    uow = SqlUnitOfWork(engine)
    return CadenceJob(
        uow=uow,
        clock=clock,
        ids=ids,
        events=_event_sink(uow, master_key, clock),
        outbox=OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids),
        source=source,
        templates=_comms(config).templates,
        tenant_id=config.tenant.id,
        integration_user=config.servicenow.integration_user,
    )


def _build_shadow_runner(  # noqa: PLR0913 — config plus the injectable ports
    config: AppConfig,
    *,
    clock: Clock,
    master_key: bytes | None = None,
    uow: SqlUnitOfWork | None = None,
    ids: IdGenerator | None = None,
    llm_provider: LLMProvider | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ShadowRunner:
    """Loop A without side effects (10-wp WP-014): the shadow-mode triage entry point."""
    uow = uow if uow is not None else _build_uow(config)
    ids = ids if ids is not None else UlidIdGenerator()
    events = _event_sink(uow, master_key, clock)
    catalog = _build_catalog(config)
    return ShadowRunner(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=CaseRepo(uow),
        classifier=_build_classifier(config, events, ids, clock, llm_provider),
        catalog=catalog,
        repo=ShadowRepo(uow),
        tenant_constants=config.redaction.tenant_constants,
        # WP-111: storm grouping and the spend breaker apply in shadow too, so
        # the counterfactual (and the LLM bill) match assisted mode.
        grouping=GroupingService(uow=uow, ids=ids, catalog=catalog, tenant_id=config.tenant.id),
        spend=SpendBreaker(uow=uow, daily_budget_tokens=config.llm.daily_budget_tokens),
        memory=_build_memory(config, uow, clock),
        # WP-112: page lookups are reads, so shadow records page_recommended too.
        paging=build_paging(config, transport=transport),
    )


def _event_sink(uow: SqlUnitOfWork, master_key: bytes | None, clock: Clock) -> SqlEventSink:
    key = master_key if master_key is not None else load_master_key_default()
    return SqlEventSink(uow=uow, cipher=Aead(master_key=key, keys=SqlKeyStore(uow), clock=clock))


def _build_servicenow_source(
    config: AppConfig, *, clock: Clock, transport: httpx.BaseTransport | None = None
) -> ServiceNowSource:
    """The read side of ServiceNow only (02 §6): shadow mode never builds a sink."""
    return ServiceNowSource(http=_servicenow_http(config, clock=clock, transport=transport))


def _servicenow_http(
    config: AppConfig,
    *,
    clock: Clock,
    transport: httpx.BaseTransport | None = None,
    max_tries: int | None = None,
) -> ServiceNowHTTP:
    """The instance's HTTP client with the configured auth (02 §1, §5)."""
    sn = config.servicenow
    auth: ServiceNowAuth
    if isinstance(sn.auth, BasicAuthConfig):
        auth = BasicAuth(
            user=sn.auth.user.get_secret_value(), password=sn.auth.password.get_secret_value()
        )
    else:
        auth = OAuthClientCredentials(
            http=httpx.Client(transport=transport, timeout=10.0),
            token_url=f"{sn.instance_url.rstrip('/')}/oauth_token.do",
            client_id=sn.auth.client_id.get_secret_value(),
            client_secret=sn.auth.client_secret.get_secret_value(),
            clock=clock,
        )
    client = build_client(sn.instance_url, transport=transport)
    if max_tries is None:
        return ServiceNowHTTP(http=client, auth=auth)
    return ServiceNowHTTP(http=client, auth=auth, max_tries=max_tries, backoff_base_s=0.2)


def require_automation_decided(config: AppConfig, store: AutomationActorStore) -> None:
    """Assisted mode needs every automation-actor candidate decided (11-wp WP-101).

    An undecided candidate counts as a human, so every ticket it auto-assigns
    would look taken over (ARCH §8.2). Shadow mode only records, so it runs
    without the decisions.
    """
    if config.mode == "shadow":
        return
    total, decided = store.summary(config.tenant.id)
    if total > decided:
        raise ConfigError(  # noqa: TRY003
            f"mode '{config.mode}' needs every automation-actor candidate confirmed or "
            f"rejected; {total - decided} undecided (admin: /ui/setup)"
        )


def _integration_ids(
    config: AppConfig, *, clock: Clock, transport: httpx.BaseTransport | None
) -> Callable[[], frozenset[str]]:
    """The values ``assigned_to`` takes for the agent: its user name and ``sys_id``.

    The ``sys_id`` is looked up once, lazily, with a read-only probe. Until the
    lookup succeeds only the user name is known; a hand-back by
    re-assignment is then missed, never invented.
    """
    name = config.servicenow.integration_user
    resolved: set[str] = set()

    def ids() -> frozenset[str]:
        if not resolved:
            try:
                probe = ServiceNowTableProbe(
                    http=_servicenow_http(config, clock=clock, transport=transport, max_tries=2)
                )
                rows = probe.read(
                    "sys_user", query=f"user_name={name}", fields=["sys_id"], limit=1
                ).rows
            except ExternalServiceError:
                rows = []
            resolved.update(r["sys_id"] for r in rows if r.get("sys_id"))
        return frozenset({name, *resolved})

    return ids


def _worker_owner() -> str:
    """The lease owner for this process (03 §2.5): host and pid."""
    return f"worker:{socket.gethostname()}:{os.getpid()}"


#: The declared result schema of every first-party tool (WP-107). Runbooks are
#: validated against these whether or not the tool is configured; an
#: unconfigured tool's step simply can't run (ARCH §5.5).
KNOWN_TOOL_SCHEMAS: Mapping[str, Mapping[str, object]] = {
    LogsTool.name: logs_tool.SCHEMA,
    CronStatusTool.name: cron_tool.SCHEMA,
    AirflowTool.name: airflow_tool.SCHEMA,
    GitHubActionsTool.name: gha_tool.SCHEMA,
    OnCallTool.name: oncall_tool.SCHEMA,
}


def build_rca_knowledge(config: AppConfig, registered: Collection[str]) -> Knowledge:
    """WP-110: runbooks (every check compiled) and the probes of registered tools."""
    taxonomy = load_hypotheses(Path(config.paths.hypotheses))
    ids = [h.id for h in taxonomy.hypotheses]
    compiler = CelCheckCompiler()
    runbooks_dir = Path(config.paths.runbooks)
    return Knowledge(
        runbooks=load_runbooks(
            runbooks_dir, hypotheses=ids, tool_schemas=KNOWN_TOOL_SCHEMAS, compiler=compiler
        ),
        probes=load_probes(
            runbooks_dir / "probes.yaml",
            hypotheses=ids,
            tool_schemas={n: s for n, s in KNOWN_TOOL_SCHEMAS.items() if n in registered},
            compiler=compiler,
        ),
        excludes={h.id: list(h.excludes) for h in taxonomy.hypotheses},
    )


def build_rca_job(  # noqa: PLR0913 — config plus the worker's ports and seams
    config: AppConfig,
    *,
    engine: Engine,
    master_key: bytes,
    clock: Clock,
    ids: IdGenerator,
    transport: httpx.BaseTransport | None = None,
    llm_provider: LLMProvider | None = None,
) -> RcaJob:
    """``rca.run`` (WP-110). RCA's own events are buffered and written with the artifact."""
    uow = SqlUnitOfWork(engine)
    buffer = BufferedEvents()
    tools = build_tool_registry(config, events=buffer, ids=ids, clock=clock, transport=transport)
    rca = config.rca
    service = RcaService(
        knowledge=build_rca_knowledge(config, tools.names),
        tools=tools,
        llm=_build_llm(config, buffer, ids, clock, llm_provider),
        events=buffer,
        ids=ids,
        clock=clock,
        prior=rca.prior,
        freeze=rca.freeze,
        max_claims=rca.max_claims,
        entail_budget_s=rca.entail_budget_s,
    )
    rows = [*config.routing, *([config.default] if config.default else [])]
    return RcaJob(
        uow=uow,
        service=service,
        catalog=_build_catalog(config),
        buffer=buffer,
        events=_event_sink(uow, master_key, clock),
        lite_routes=lite_route_ids({row.id: row.rca for row in rows}),
        default_budget=RcaBudget(
            max_tool_calls=rca.max_tool_calls,
            max_tokens=rca.max_tokens,
            max_hypotheses=rca.max_hypotheses,
        ),
        ids=ids,
        clock=clock,
        tenant_id=config.tenant.id,
    )


def _housekeeping_jobs(  # noqa: PLR0913 — the worker's shared ports
    config: AppConfig,
    *,
    engine: Engine,
    key: bytes,
    clock: Clock,
    ids: IdGenerator,
    lease: SqlLease,
    owner: str,
) -> list[ScheduledJob]:
    """BL-028: ``proposals.expire`` and ``retention.sweep`` (02 §7), each on its own UoW."""
    gate_uow = SqlUnitOfWork(engine)
    gate = GateService(
        uow=gate_uow,
        clock=clock,
        ids=ids,
        events=_event_sink(gate_uow, key, clock),
        catalog=_build_catalog(config),
        ttl=timedelta(hours=config.proposals.ttl_hours),
    )
    sweep_uow = SqlUnitOfWork(engine)
    retention = RetentionService(
        uow=sweep_uow,
        store=SqlRetention(sweep_uow),
        events=_event_sink(sweep_uow, key, clock),
        ids=ids,
        clock=clock,
        tenant_id=config.tenant.id,
        events_days=config.retention.events_days,
        alerts_days=config.retention.alerts_days,
        comms_days=config.retention.comms_days,
    )
    return [
        ScheduledJob(
            name=PROPOSALS_EXPIRE_LEASE,
            interval_s=float(PROPOSALS_EXPIRE_INTERVAL_S),
            run=lambda: proposals_expire(
                expire=lambda: gate.expire_due(now=clock.now()), lease=lease, owner=owner
            ),
        ),
        ScheduledJob(
            name=RETENTION_SWEEP_LEASE,
            interval_s=float(RETENTION_SWEEP_INTERVAL_S),
            run=lambda: retention_sweep(
                sweep=lambda: sum(retention.sweep().values()), lease=lease, owner=owner
            ),
        ),
    ]


def build_worker(  # noqa: PLR0913 — config plus the test seams
    config: AppConfig,
    *,
    master_key: bytes | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    transport: httpx.BaseTransport | None = None,
    llm_provider: LLMProvider | None = None,
    engine: Engine | None = None,
) -> Worker:
    """The shadow worker (02 §7, 10-wp WP-008/014): one ``sync.poll.<table>`` per table.

    Each poll feeds ``ShadowInbound``: a new record runs through the shadow
    runner, a change to a known record writes one ``sync`` event. v0.0 is
    shadow-only (02 §1), so no ``TicketSink`` and no ``outbox.execute`` job is
    built: nothing can reach ServiceNow except the read-only poll.
    ``transport`` / ``llm_provider`` / ``engine`` are test seams.
    """
    engine = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    require_automation_decided(config, SqlAutomationActors(engine))
    if config.mode != "shadow":
        raise ConfigError(  # noqa: TRY003 — the config mode gate already enforces this (02 §1)
            f"v0.0 accepts only mode 'shadow'; got '{config.mode}' (02 §1, CAP §4.7)"
        )
    clock = clock if clock is not None else SystemClock()
    ids = ids if ids is not None else UlidIdGenerator()
    key = master_key if master_key is not None else load_master_key_default()
    runner_uow = SqlUnitOfWork(engine)
    runner = _build_shadow_runner(
        config,
        clock=clock,
        master_key=key,
        uow=runner_uow,
        ids=ids,
        llm_provider=llm_provider,
        transport=transport,
    )
    inbound_events = _event_sink(runner_uow, key, clock)
    inbound = ShadowInbound(
        runner=runner,
        uow=runner_uow,
        cases=CaseRepo(runner_uow),
        events=inbound_events,
        ids=ids,
        clock=clock,
        tenant_id=config.tenant.id,
        memory=_build_memory(config, runner_uow, clock),
        feedback=FeedbackService(
            uow=runner_uow,
            clock=clock,
            ids=ids,
            events=inbound_events,
            tenant_id=config.tenant.id,
            integration_user=config.servicenow.integration_user,
            classification_map=config.servicenow.classification_map,
        ),
        lifecycle=LifecycleService(
            uow=runner_uow,
            clock=clock,
            ids=ids,
            events=inbound_events,
            tenant_id=config.tenant.id,
            integration_user=config.servicenow.integration_user,
            integration_ids=_integration_ids(config, clock=clock, transport=transport),
        ),
    )
    sync_uow = SqlUnitOfWork(engine)
    sn = config.servicenow
    sync = SyncService(
        tenant_id=config.tenant.id,
        uow=sync_uow,
        repo=SyncRepo(sync_uow),
        source=_build_servicenow_source(config, clock=clock, transport=transport),
        handler=inbound,
        clock=clock,
        integration_user=sn.integration_user,
        to_alert=record_to_alert,
        page_size=sn.page_size,
        overlap_s=sn.overlap_s,
    )
    lease = SqlLease(SqlUnitOfWork(engine), clock)
    owner = _worker_owner()

    def poll(table: SnTable) -> Callable[[], JobResult]:
        return lambda: sync_poll(poller=sync, table=table, lease=lease, owner=owner)

    jobs = [
        ScheduledJob(
            name=sync_poll_lease(SnTable(name)),
            interval_s=float(sn.poll_interval_s),
            run=poll(SnTable(name)),
        )
        for name in sn.tables
    ]
    if config.webhooks:
        inbox_uow = SqlUnitOfWork(engine)
        inbox_cipher = Aead(master_key=key, keys=SqlKeyStore(inbox_uow), clock=clock)
        drainer = WebhookDrainer(
            uow=inbox_uow,
            inbox=WebhookInbox(inbox_uow, inbox_cipher),
            runner=runner,
            clock=clock,
            tenant_id=config.tenant.id,
        )
        jobs.append(
            ScheduledJob(
                name=WEBHOOKS_DRAIN_LEASE,
                interval_s=float(WEBHOOKS_DRAIN_INTERVAL_S),
                run=lambda: webhooks_drain(drain=drainer.run_once, lease=lease, owner=owner),
            )
        )
    grouping = GroupingService(
        uow=SqlUnitOfWork(engine),
        ids=ids,
        catalog=_build_catalog(config),
        tenant_id=config.tenant.id,
    )
    jobs.append(
        ScheduledJob(
            name=GROUPS_CLOSE_LEASE,
            interval_s=float(GROUPS_CLOSE_INTERVAL_S),
            run=lambda: groups_close(
                close=lambda: grouping.close_due(clock.now()), lease=lease, owner=owner
            ),
        )
    )
    jobs.extend(
        _housekeeping_jobs(
            config, engine=engine, key=key, clock=clock, ids=ids, lease=lease, owner=owner
        )
    )
    if any(row.rca == "lite" for row in [*config.routing, *filter(None, [config.default])]):
        rca_job = build_rca_job(
            config,
            engine=engine,
            master_key=key,
            clock=clock,
            ids=ids,
            transport=transport,
            llm_provider=llm_provider,
        )
        jobs.append(
            ScheduledJob(
                name=RCA_RUN_LEASE,
                interval_s=float(RCA_RUN_INTERVAL_S),
                run=lambda: rca_run(run=rca_job.run_once, lease=lease, owner=owner),
            )
        )
    if config.llm.deprecation_feed:
        feed = load_deprecation_feed(Path(config.llm.deprecation_feed))
        slots = {name: slot.model_id for name, slot in config.llm.slots.items()}
        jobs.append(
            ScheduledJob(
                name=LLM_DEPRECATION_LEASE,
                interval_s=float(LLM_DEPRECATION_INTERVAL_S),
                run=lambda: llm_deprecation_check(
                    notices=lambda: deprecation_notices(slots, feed, clock.now().date()),
                    lease=lease,
                    owner=owner,
                ),
            )
        )
    return Worker(jobs)


def build_stats(config: AppConfig) -> StatsService:
    """``StatsService`` on the configured database (10-wp WP-014)."""
    return StatsService(uow=_build_uow(config), clock=SystemClock(), tenant_id=config.tenant.id)


def shadow_import(config: AppConfig, export: Path) -> list[ShadowRecord]:
    """``shadow import``: replay a partner export through shadow at each ``opened_at``."""
    clock = ReplayClock()
    runner = _build_shadow_runner(config, clock=clock)
    incidents = read_export(export, tenant_id=config.tenant.id)
    return import_history(incidents, runner=runner, clock=clock)


def build_container(config: AppConfig) -> dict[str, Callable[[], object]]:
    """Build the v0.0 object graph as named lazy factories (10-wp WP-011).

    Shadow mode only (02 §1, CAP §4.7): v0.0 never constructs the ServiceNow
    sink, so the graph carries no ``TicketSink`` entry. Every entry is a
    zero-arg factory — file I/O, the engine, and key loading happen at call
    time, never at build time.
    """
    if config.mode != "shadow":
        raise ConfigError(  # noqa: TRY003 — the config mode gate already enforces this (02 §1)
            f"v0.0 accepts only mode 'shadow'; got '{config.mode}' (02 §1, CAP §4.7)"
        )
    return {
        "config": lambda: config,
        "uow": lambda: _build_uow(config),
        "Catalog": lambda: _build_catalog(config),
        "RoutingTable": lambda: _build_routing(config),
        "CalibrationTable": lambda: _build_calibration(config),
        # Shadow mode swaps the ShadowRunner in for TriageService (10-wp WP-011):
        # same Classifier and build_action_specs, no gate submit, no outbox.
        "ShadowRunner": lambda: _build_shadow_runner(config, clock=SystemClock()),
        "StatsService": lambda: build_stats(config),
        # The shadow worker (02 §7): read-only ServiceNow poll → ShadowInbound.
        "Worker": lambda: build_worker(config),
    }


class _NullEvents:
    """The doctor's LLM probe runs outside any trace: it records no events."""

    def append(self, event: object) -> None:
        del event


class _LazyModelProbe:
    """Builds the LLM client on first probe, so a provider wiring error is the ``llm`` fail."""

    def __init__(self, factory: Callable[[], LLMClient]) -> None:
        self._factory = factory
        self._client: LLMClient | None = None

    def probe(self, model_id: str) -> str:
        if self._client is None:
            self._client = self._factory()
        return self._client.probe(model_id)


def _head_revision() -> str:
    """The newest migration shipped with this build (03 §4)."""
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    head = script.get_current_head()
    if head is None:  # pragma: no cover — the build always ships migrations
        raise ConfigError("no migrations found (03 §4)")  # noqa: TRY003
    return head


def _load_bundle(config: AppConfig) -> Bundle:
    paths = config.paths
    return Bundle(
        catalog=load_catalog(Path(paths.catalog)),
        taxonomy=load_classification(Path(paths.taxonomy)),
        hypotheses=load_hypotheses(Path(paths.hypotheses)),
    )


def build_doctor_context(  # noqa: PLR0913 — config plus the test seams
    config: AppConfig,
    *,
    discover_automation: bool = False,
    clock: Clock | None = None,
    engine: Engine | None = None,
    transport: httpx.BaseTransport | None = None,
    llm_provider: LLMProvider | None = None,
    identity: DiscoveryProbe | None = None,
) -> DoctorContext:
    """Everything ``supportagent doctor`` reads (02 §4.1), as read-only probes.

    ServiceNow is reached through ``ServiceNowTableProbe`` only (GET; there is
    no write method), with at most two tries so a dead instance fails fast.
    """
    clock = clock if clock is not None else SystemClock()
    engine = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    ids = UlidIdGenerator()
    oidc: DiscoveryProbe = identity or OIDCClient(
        issuer=config.identity.oidc_issuer,
        client_id=config.identity.client_id.get_secret_value(),
        client_secret=config.identity.client_secret.get_secret_value(),
        redirect_url=config.identity.redirect_url,
        http=httpx.Client(timeout=10.0, transport=transport),
        clock=clock,
    )
    return DoctorContext(
        config=config,
        load_bundle=lambda: _load_bundle(config),
        load_calibration=lambda: _build_calibration(config),
        schema=SqlSchemaProbe(engine, head=_head_revision()),
        servicenow=ServiceNowTableProbe(
            http=_servicenow_http(config, clock=clock, transport=transport, max_tries=2)
        ),
        llm=_LazyModelProbe(lambda: _build_llm(config, _NullEvents(), ids, clock, llm_provider)),
        identity=oidc,
        automation=SqlAutomationActors(engine),
        clock=clock,
        discover_automation=discover_automation,
    )


def run_doctor(config: AppConfig, *, discover_automation: bool = False) -> DoctorReport:
    """``supportagent doctor``: every 02 §4.1 check against the configured deployment."""
    ctx = build_doctor_context(config, discover_automation=discover_automation)
    return Doctor(default_checks()).run(ctx)


def request_default_grants(config: AppConfig, *, master_key: bytes | None = None) -> list[str]:
    """``doctor --setup``: write the default T1 grant requests (ARCH §6.4); idempotent."""
    key = master_key if master_key is not None else load_master_key_default()
    engine = make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(engine)
        clock = SystemClock()
        setup = SetupService(
            uow=uow,
            clock=clock,
            ids=UlidIdGenerator(),
            events=_event_sink(uow, key, clock),
            catalog=_build_catalog(config),
            tenant_id=config.tenant.id,
        )
        return setup.request_default_grants(requested_by=config_marker(config))
    finally:
        engine.dispose()


class _RecordingProvider:
    """Records each (redacted) request the provider receives (eval INV-06 check)."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.requests: list[str] = []

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request.system + "\n" + request.user)
        return self._inner.complete_json(request)


def _wiring_calibration(config: AppConfig) -> CalibrationTable:
    """Every taxonomy label fully calibrated, so the harness exercises routing.

    The smoke suite scores the pipeline's wiring; the production gate reads
    the eval-produced artifact (ADR-0002), which this never replaces.
    """
    strong = CalibrationCountRow(n=1000, correct=1000)
    labels = load_classification(Path(config.paths.taxonomy)).labels
    return CalibrationTable(
        grade="smoke",
        snapshot_id="wiring",
        model_id=config.llm.slots[SlotName.CLASSIFY.value].model_id,
        labels={label.id: CalibrationLabelRow(agree=strong, abstain=strong) for label in labels},
    )


class EvalPipeline:
    """Shadow triage for the eval harness (10-wp WP-016) on a throwaway database.

    ``fixture`` mode scripts the LLM per case; ``live`` uses the configured
    provider. Either way every request passes the redactor first and is
    recorded for the case's INV-06 check. Nothing is proposed or sent: the
    harness reads the shadow record (decision + the specs that would have
    been proposed).
    """

    def __init__(self, config: AppConfig, workdir: Path, *, live: bool) -> None:
        self.mode: Literal["fixture", "live"] = "live" if live else "fixture"
        self.model_id = config.llm.slots[SlotName.CLASSIFY.value].model_id
        url = f"sqlite:///{(workdir / 'eval.db').as_posix()}"
        run_migrations(url)
        self._engine = make_engine(url)
        uow = SqlUnitOfWork(self._engine)
        self._clock = ReplayClock()
        ids = UlidIdGenerator()
        events = _event_sink(uow, secrets.token_bytes(32), self._clock)
        self._fixture = None if live else FixtureLLMProvider()
        self._recorder: _RecordingProvider | None = None

        def record(inner: LLMProvider) -> LLMProvider:
            self._recorder = _RecordingProvider(inner)
            return self._recorder

        llm = _build_llm(
            config, events, ids, self._clock, self._fixture, decorate=record, resilient=False
        )
        self._runner = ShadowRunner(
            uow=uow,
            clock=self._clock,
            ids=ids,
            events=events,
            case_repo=CaseRepo(uow),
            classifier=Classifier(
                rule_set=_build_ruleset(config),
                llm=llm,
                calibration=_wiring_calibration(config),
                min_score=config.calibration.min_score,
                routing=_build_routing(config),
            ),
            catalog=_build_catalog(config),
            repo=ShadowRepo(uow),
            tenant_constants=config.redaction.tenant_constants,
        )
        self._seen: set[str] = set()
        self._seen_before: set[str] = set()

    def run(self, case: EvalCase, evt: AlertEvent) -> CaseRun:
        if self._fixture is not None:
            self._fixture.load([_fixture_answer(case.fixture_answer())])
        if self._recorder is not None:
            self._recorder.requests.clear()
        record = self._deliver(case, evt)
        for _ in range(case.repeat - 1):
            record = self._deliver(case, evt)
        # Linked: the last delivery landed on a case that already existed.
        linked = record.case_id in self._seen_before
        decision = TriageDecision.model_validate_json(json.dumps(record.proposed["decision"]))
        specs = record.proposed.get("specs", [])
        return CaseRun(
            case_id=case.id,
            linked=linked,
            decision=decision,
            actions=[str(spec["kind"]) for spec in specs] if isinstance(specs, list) else [],
            llm_requests=list(self._recorder.requests) if self._recorder else [],
        )

    def _deliver(self, case: EvalCase, evt: AlertEvent) -> ShadowRecord:
        self._clock.set(evt.received_at)
        record = self._runner.run(evt)
        if record is None:  # pragma: no cover — shadow cases always carry a record
            raise RuntimeError(f"case {case.id}: no shadow record")  # noqa: TRY003
        self._seen_before = set(self._seen)
        self._seen.add(record.case_id)
        return record

    def close(self) -> None:
        self._engine.dispose()


def _fixture_answer(spec: FixtureSpec) -> FixtureAnswer:
    if spec.fault is not None:
        return FixtureAnswer(fault=spec.fault)
    if spec.raw is not None:
        return FixtureAnswer(raw=spec.raw)
    if spec.classification is None or spec.severity is None:
        # Nothing scripted: the call fails closed, like an LLM outage.
        return FixtureAnswer(fault="unavailable")
    return FixtureAnswer.triage(spec.classification, spec.severity)


class QualityPipeline:
    """The quality suite's sampler (11-wp WP-104): the configured classifier, cached.

    ``live`` calls the configured model through the response cache
    (``evals/.cache/``, keyed per ARCH §11, sample index included). Otherwise
    the fixture LLM scripts each case, which tests wiring only. The classifier
    is the production one (pre-classifier rules, calibration from config,
    routing), but no case is persisted: each sample is a fresh decision.
    """

    def __init__(self, config: AppConfig, *, live: bool, snapshot_id: str, cache_dir: Path) -> None:
        self.mode = "live" if live else "fixture"
        self.model_id = config.llm.slots[SlotName.CLASSIFY.value].model_id
        self._catalog = _build_catalog(config)
        self._clock = SystemClock()
        self._fixture = None if live else FixtureLLMProvider()
        self._cache: CachingLLMProvider | None = None

        def cached(inner: LLMProvider) -> LLMProvider:
            self._cache = CachingLLMProvider(inner, directory=cache_dir, snapshot_id=snapshot_id)
            return self._cache

        llm = _build_llm(
            config,
            _NullEvents(),
            UlidIdGenerator(),
            self._clock,
            self._fixture,
            decorate=cached if live else None,
            resilient=False,
        )
        self._classifier = Classifier(
            rule_set=_build_ruleset(config),
            llm=llm,
            calibration=_wiring_calibration(config),
            min_score=config.calibration.min_score,
            routing=_build_routing(config),
        )

    @property
    def cache_stats(self) -> tuple[int, int]:
        return (self._cache.hits, self._cache.misses) if self._cache else (0, 0)

    def preclass(self, case: EvalCase, index: int) -> str | None:
        """The deterministic pre-classifier's label (agree vs abstain row, WP-105)."""
        return self._classifier.rule_set.classify(case.alert(index))

    def sample(self, case: EvalCase, index: int, sample: int) -> tuple[str | None, str | None]:
        if self._fixture is not None:
            self._fixture.load([_fixture_answer(case.fixture_answer())])
        if self._cache is not None:
            self._cache.sample = sample
        evt = case.alert(index)
        service = self._catalog.resolve_service(resource=evt.resource, cmdb_ci=evt.cmdb_ci)
        decision = self._classifier.decide(
            evt,
            Case(
                id=f"quality-{case.id}",
                tenant_id=evt.tenant_id,
                fingerprint=f"quality-{case.id}",
                origin=evt.origin,
                service=service,
                external_table=evt.table,
                external_id=evt.external_id,
                severity=evt.severity_hint,
            ),
            TraceContext.new(evt.tenant_id, f"tr_quality_{case.id}_{sample}", "quality"),
        )
        return decision.classification, (decision.severity.value if decision.severity else None)


def detector_recall(path: Path) -> RecallReport:
    """ARCH §6.1 quality metric: share of held-out secrets the redactor removes."""
    redactor = Redactor(detectors=built_in_detectors())
    per: dict[str, list[int]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        redacted = redactor.redact(str(row["text"])).text
        for secret in row["secrets"]:
            cell = per.setdefault(str(secret["class"]), [0, 0])
            cell[1] += 1
            cell[0] += int(str(secret["value"]) not in redacted)
    detected = sum(d for d, _ in per.values())
    total = sum(t for _, t in per.values())
    return RecallReport(
        total=total, detected=detected, by_class={k: (d, t) for k, (d, t) in per.items()}
    )


def split_problems(splits_path: Path, cases: Sequence[EvalCase]) -> list[str]:
    """ROADMAP §2.1 / ARCH §10.1: ``evals/splits.yaml`` keeps train and holdout disjoint.

    ``{train: [case ids], holdout: [case ids]}``: every holdout case is later
    than every training case (by its input ``received_at``) and shares no
    normalized error signature with one.
    """
    doc = load_yaml_document(splits_path)
    by_id = {c.id: (i, c) for i, c in enumerate(cases)}

    def side(name: str) -> list[SplitCase]:
        out: list[SplitCase] = []
        for case_id in doc.get(name) or []:
            if case_id not in by_id:
                continue
            index, case = by_id[case_id]
            evt = case.alert(index)
            sig = normalize(evt.error_signature) if evt.error_signature else None
            out.append(SplitCase(case_id=case.id, opened_at=evt.received_at, signature=sig))
        return out

    return split_violations(side("train"), side("holdout"))


def quality_gates(config: AppConfig) -> tuple[QualityGate, ...]:
    path = Path(config.paths.gates)
    return load_quality_gates(path) if path.is_file() else DEFAULT_GATES


def create_snapshot(config: AppConfig, snapshot_id: str, root: Path) -> Path:
    """``eval snapshot create`` (ARCH §11): freeze what a rendered prompt depends on.

    Copies the catalog, the taxonomies, the pre-classifier rules and the
    prompts into ``root/<id>/`` with a manifest of their sha256s. A run
    pinned to the snapshot keys its cache on the id, so a later catalog or
    prompt edit can't silently change a pinned result.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,63}", snapshot_id):
        raise ConfigError(f"bad snapshot id {snapshot_id!r}")  # noqa: TRY003
    out = root / snapshot_id
    if out.exists():
        raise ConfigError(f"snapshot {snapshot_id} exists; snapshots are immutable")  # noqa: TRY003
    sources = {
        "catalog.yaml": Path(config.paths.catalog),
        "classification.yaml": Path(config.paths.taxonomy),
        "hypotheses.yaml": Path(config.paths.hypotheses),
        "preclass_rules.yaml": Path(config.paths.taxonomy).parent / "preclass_rules.yaml",
    }
    out.mkdir(parents=True)
    files: dict[str, str] = {}
    for name, src in sources.items():
        data = src.read_bytes()
        (out / name).write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    prompts = Path(config.paths.prompts)
    for prompt in sorted(prompts.rglob("*.md")):
        rel = Path("prompts") / prompt.relative_to(prompts)
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        data = prompt.read_bytes()
        (out / rel).write_bytes(data)
        files[rel.as_posix()] = hashlib.sha256(data).hexdigest()
    manifest = {"id": snapshot_id, "created": wall_clock().now().isoformat(), "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def build_tool_registry(
    config: AppConfig,
    *,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
    transport: httpx.BaseTransport | None = None,
) -> ToolRegistry:
    """WP-107: the configured read tools behind the registry (class/tier checked at load).

    ``gha_500`` (``SUPPORTAGENT_FAULT_INJECT``) makes the GitHub Actions tool
    fail as if its API were down (WP-106).
    """
    cfg = config.tools
    catalog = _build_catalog(config)
    tools: list[Tool] = []
    if cfg.logs_root:
        allow = frozenset(p for s in catalog.services for p in s.log_paths)
        tools.append(LogsTool(root=Path(cfg.logs_root), allowlist=allow))
    if cfg.cron_status_dir:
        tools.append(CronStatusTool(status_dir=Path(cfg.cron_status_dir), clock=clock))

    def client(base_url: str, headers: Mapping[str, str]) -> httpx.Client:
        return httpx.Client(
            base_url=base_url, headers=dict(headers), transport=transport, timeout=10.0
        )

    if cfg.airflow:
        auth = (
            {"Authorization": f"Bearer {cfg.airflow.token.get_secret_value()}"}
            if (cfg.airflow.token)
            else {}
        )
        tools.append(AirflowTool(client=client(cfg.airflow.base_url, auth)))
    if cfg.github:
        auth = {"Accept": "application/vnd.github+json"}
        if cfg.github.token:
            auth["Authorization"] = f"Bearer {cfg.github.token.get_secret_value()}"
        gha: Tool = GitHubActionsTool(client=client(cfg.github.base_url, auth))
        if Fault.GHA_500 in load_fault_injection_default():
            gha = FaultInjectingTool(gha)
        tools.append(gha)
    if cfg.oncall:
        oc = cfg.oncall
        pd = (
            client(
                oc.pagerduty_base_url,
                {"Authorization": f"Token token={oc.pagerduty_token.get_secret_value()}"},
            )
            if oc.pagerduty_token
            else None
        )
        og = (
            client(
                oc.opsgenie_base_url,
                {"Authorization": f"GenieKey {oc.opsgenie_token.get_secret_value()}"},
            )
            if oc.opsgenie_token
            else None
        )
        tools.append(OnCallTool(pagerduty=pd, opsgenie=og))
    redactor = Redactor(detectors=built_in_detectors())
    return ToolRegistry(
        tools, events=events, ids=ids, clock=clock, redact=lambda text: redactor.redact(text).text
    )


def wall_clock() -> Clock:
    """The system clock, for entrypoints that label output with the time."""
    return SystemClock()


def eval_config(config_path: Path, *, live: bool, catalog: Path) -> AppConfig:
    """The eval run's config: offline (fixture, no secrets) unless ``live``.

    The catalog is the manifest's: the cases are written against it.
    """
    cfg = load_config_default(config_path) if live else load_config_offline(config_path)
    paths = cfg.paths.model_copy(update={"catalog": catalog.as_posix()})
    return cfg.model_copy(update={"paths": paths})


def load_eval_suite(manifest_path: Path) -> tuple[list[EvalCase], Manifest]:
    """The manifest and its case files (YAML read at the file boundary, ADR-0004)."""
    manifest = Manifest.model_validate(load_yaml_document(manifest_path))
    cases_dir = manifest_path.parent / manifest.cases
    return parse_cases(load_eval_documents(cases_dir)), manifest


def config_marker(config: AppConfig) -> str:
    """``config:<sha>`` — the ``requested_by`` of grants written from config (ARCH §6.4).

    The digest covers the resolved config with secrets masked (pydantic dumps
    ``SecretStr`` as ``**********``), so it identifies the config, not its keys.
    """
    digest = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
    return f"config:{digest[:12]}"


def api_context_for(  # noqa: PLR0913 — every port the API needs, injectable for tests
    engine: Engine,
    *,
    tenant_id: str,
    catalog: Catalog,
    master_key: bytes,
    clock: Clock,
    ids: IdGenerator,
    identity: IdentityProvider,
    session_secret: str,
    secure_cookies: bool,
    config_marker: str,
    proposal_ttl: timedelta = timedelta(hours=24),
    idp_label: str = "your identity provider",
    webhook_sources: Mapping[str, WebhookSource] | None = None,
    promotion_gate: PromotionGate | None = None,
) -> ApiContext:
    """The API's per-request factories over one engine (10-wp WP-012/013).

    Each factory call builds a fresh UoW with its own key store and event sink,
    so concurrent requests never share a connection.
    """

    def _ports() -> tuple[SqlUnitOfWork, SqlEventSink]:
        uow = SqlUnitOfWork(engine)
        aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
        return uow, SqlEventSink(uow=uow, cipher=aead)

    def setup() -> SetupService:
        uow, events = _ports()
        return SetupService(
            uow=uow, clock=clock, ids=ids, events=events, catalog=catalog, tenant_id=tenant_id
        )

    def approvals() -> ApprovalService:
        uow, events = _ports()
        gate = GateService(
            uow=uow, clock=clock, ids=ids, events=events, catalog=catalog, ttl=proposal_ttl
        )
        writer = OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids)
        return ApprovalService(
            uow=uow,
            gate=gate,
            outbox=writer,
            tenant_id=tenant_id,
            feedback=FeedbackService(
                uow=uow, clock=clock, ids=ids, events=events, tenant_id=tenant_id
            ),
        )

    def queries() -> AdminQueries:
        return AdminQueries(uow=SqlUnitOfWork(engine), tenant_id=tenant_id)

    def stats() -> StatsService:
        return StatsService(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant_id)

    def replay(trace_id: str) -> ReplayView:
        uow, events = _ports()
        with uow:  # read-only: never commits
            return ReplayService(repo=events).replay(trace_id)

    def comms_plans() -> CommsPlans:
        uow, events = _ports()
        return CommsPlans(uow=uow, clock=clock, ids=ids, events=events, tenant_id=tenant_id)

    def webhooks() -> WebhookService:
        uow = SqlUnitOfWork(engine)
        aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
        return WebhookService(
            uow=uow,
            clock=clock,
            ids=ids,
            events=SqlEventSink(uow=uow, cipher=aead),
            nonces=SqlNonceStore(uow),
            inbox=WebhookInbox(uow, aead),
            sources=webhook_sources or {},
            tenant_id=tenant_id,
        )

    def lifecycle() -> LifecycleService:
        # The API only hands back; ServiceNow-driven transitions run in the worker.
        uow, events = _ports()
        return LifecycleService(uow=uow, clock=clock, ids=ids, events=events, tenant_id=tenant_id)

    return ApiContext(
        setup=setup,
        approvals=approvals,
        queries=queries,
        stats=stats,
        replay=replay,
        lifecycle=lifecycle,
        comms_plans=comms_plans,
        webhooks=webhooks,
        promotion_gate=promotion_gate or PromotionGate(),
        tenant_id=tenant_id,
        identity=identity,
        session_secret=session_secret,
        secure_cookies=secure_cookies,
        config_marker=config_marker,
        clock=clock,
        idp_label=idp_label,
    )


def build_api_context(
    config: AppConfig,
    *,
    identity: IdentityProvider | None = None,
    master_key: bytes | None = None,
) -> ApiContext:
    """The API's wiring from config (10-wp WP-012). ``identity`` overrides OIDC (FakeOIDC)."""
    clock = SystemClock()
    key = master_key if master_key is not None else load_master_key_default()
    provider: IdentityProvider = identity or OIDCClient(
        issuer=config.identity.oidc_issuer,
        client_id=config.identity.client_id.get_secret_value(),
        client_secret=config.identity.client_secret.get_secret_value(),
        redirect_url=config.identity.redirect_url,
        http=httpx.Client(timeout=10.0),
        clock=clock,
    )
    return api_context_for(
        make_engine(config.database.url.get_secret_value()),
        tenant_id=config.tenant.id,
        catalog=_build_catalog(config),
        master_key=key,
        clock=clock,
        ids=UlidIdGenerator(),
        identity=provider,
        session_secret=config.identity.session_secret.get_secret_value(),
        secure_cookies=config.identity.redirect_url.startswith("https://"),
        config_marker=config_marker(config),
        proposal_ttl=timedelta(hours=config.proposals.ttl_hours),
        idp_label=urlparse(config.identity.oidc_issuer).hostname or "your identity provider",
        webhook_sources=_webhook_sources(config),
        promotion_gate=_promotion_gate(config),
    )


def _promotion_gate(config: AppConfig) -> PromotionGate:
    """``paths.gates`` when present; otherwise the CAP §4.7 provisional defaults."""
    path = Path(config.paths.gates)
    return load_promotion_gate(path) if path.is_file() else PromotionGate()


def _webhook_sources(config: AppConfig) -> dict[str, WebhookSource]:
    return {
        w.source: WebhookSource(secret=w.secret.get_secret_value().encode(), mapping=w.mapping)
        for w in config.webhooks
    }


def request_grants_from_config(config: AppConfig, *, master_key: bytes | None = None) -> list[str]:
    """``grants request --from config``: expand patterns, write ``requested`` rows (ARCH §6.4)."""
    key = master_key if master_key is not None else load_master_key_default()
    catalog = _build_catalog(config)
    engine = make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(engine)
        clock = SystemClock()
        aead = Aead(master_key=key, keys=SqlKeyStore(uow), clock=clock)
        setup = SetupService(
            uow=uow,
            clock=clock,
            ids=UlidIdGenerator(),
            events=SqlEventSink(uow=uow, cipher=aead),
            catalog=catalog,
            tenant_id=config.tenant.id,
        )
        literals = expand_grant_requests(config.grants_request, catalog)
        return setup.request_grants(literals, requested_by=config_marker(config))
    finally:
        engine.dispose()


def _migrations_root() -> Path:
    """The directory holding ``alembic.ini`` + ``migrations/`` (03 §4).

    A source checkout resolves relative to this file. An installed wheel (the
    Docker image) has no source tree, so the image ships both next to its
    working directory and they resolve from there.
    """
    for root in (_REPO_ROOT, Path.cwd()):
        if (root / "alembic.ini").is_file() and (root / "migrations").is_dir():
            return root
    raise ConfigError(  # noqa: TRY003
        "alembic.ini and migrations/ not found in the source checkout or the "
        "working directory (03 §4)"
    )


def replay_trace(config: AppConfig, master_key: bytes, trace_id: str) -> ReplayView:
    """Wire the event read path and replay one trace (02 §4, 10-wp WP-004).

    Read-only: the UoW never commits, and the engine is disposed before
    returning. Raises ``NotFound`` (CLI: exit 4, 02 §4) when the log has no
    events for ``trace_id``.
    """
    engine = make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(engine)
        with uow:
            cipher = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=SystemClock())
            repo = SqlEventSink(uow=uow, cipher=cipher)
            return ReplayService(repo=repo).replay(trace_id)
    finally:
        engine.dispose()


def _alembic_config(url: str) -> Config:
    """Alembic config bound to this checkout (03 §4: one history, both dialects).

    The URL is supplied at call time — it is never read from a checked-in
    file (05 §6). The ini file is used for its logging configuration; the
    script location and URL are overridden with absolute paths so the
    command works from any working directory.
    """
    root = _migrations_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(root))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def run_migrations(
    url: str,
    revision: str = "head",
    *,
    allow_downgrade: bool = False,
) -> None:
    """Apply the migration history up to ``revision`` (02 §4, 03 §4).

    ``revision="head"`` (the default) upgrades. Any other target is a
    downgrade and is refused unless ``allow_downgrade`` is set (02 §4:
    "refuses downgrade without ``--allow-downgrade``", exit 2; 03 §4:
    downgrades below a release tag are refused without the flag).
    """
    cfg = _alembic_config(url)
    if revision != "head":
        if not allow_downgrade:
            raise DomainValidationError(  # noqa: TRY003
                "refusing to move the schema down; re-run with "
                "--allow-downgrade to override (02 §4)"
            )
        try:
            command.downgrade(cfg, revision)
        except SQLAlchemyError as exc:
            raise ExternalServiceError(f"database downgrade failed: {exc}") from exc  # noqa: TRY003
        return
    try:
        command.upgrade(cfg, "head")
    except SQLAlchemyError as exc:
        raise ExternalServiceError(f"database migration failed: {exc}") from exc  # noqa: TRY003
