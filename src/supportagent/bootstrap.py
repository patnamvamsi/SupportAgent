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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import anthropic
import httpx
import openai
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp

from supportagent.adapters.cel.check import CelCheckCompiler
from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.engine import make_engine
from supportagent.adapters.db.probe import SqlSchemaProbe
from supportagent.adapters.db.repos.automation import SqlAutomationActors
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.doctor import SqlDoctorReports
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.leases import SqlLease
from supportagent.adapters.db.repos.outbox import OutboxRepo
from supportagent.adapters.db.repos.retention import SqlRetention
from supportagent.adapters.db.repos.shadow import ShadowRepo
from supportagent.adapters.db.repos.sync import SyncRepo
from supportagent.adapters.db.repos.webhooks import SqlNonceStore, WebhookInbox
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.demo_servicenow import build as _build_demo_servicenow
from supportagent.adapters.fakes.faults import FaultInjectingTicketSink, FaultInjectingTool
from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.comms_loader import CommsCatalog, load_comms_catalog
from supportagent.adapters.files.deprecation_loader import load_deprecation_feed
from supportagent.adapters.files.eval_loader import (
    load_eval_documents,
    load_front_matter,
    load_yaml_document,
    load_yaml_records,
)
from supportagent.adapters.files.gates_loader import load_promotion_gate, load_quality_gates
from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.adapters.files.knowledge_loader import load_markdown_source, split_front_matter
from supportagent.adapters.files.pack_loader import load_pack_set
from supportagent.adapters.files.plugin_loader import LoadedPlugin, load_registry, write_index
from supportagent.adapters.files.runbook_loader import load_probes, load_runbooks
from supportagent.adapters.files.workspace import Workspace
from supportagent.adapters.gitsource.reader import GitHubReader, read_local
from supportagent.adapters.identity.oidc import OIDCClient
from supportagent.adapters.install import probes as _probes
from supportagent.adapters.knowledge.confluence import (
    ConfluenceLoad,
    export_confluence_space,
    load_confluence_space,
    site_root,
)
from supportagent.adapters.llm.client import (
    OLLAMA_DEFAULT_BASE_URL,
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
from supportagent.adapters.servicenow.history_export import export_history
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.mapping import record_to_alert
from supportagent.adapters.servicenow.probe import ServiceNowTableProbe
from supportagent.adapters.servicenow.semantics import (
    SERVICENOW_MANIFEST,
    ServiceNowSemantics,
    severity_from_priority,
)
from supportagent.adapters.servicenow.sink import ServiceNowSink
from supportagent.adapters.servicenow.source import ServiceNowSource
from supportagent.adapters.system.clock import ReplayClock, SystemClock
from supportagent.adapters.system.ids import UlidIdGenerator
from supportagent.adapters.tools.airflow import AirflowTool
from supportagent.adapters.tools.capabilities import CAPABILITIES
from supportagent.adapters.tools.changes import RecentChangesTool
from supportagent.adapters.tools.cron import CronStatusTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.kubernetes import PodStatusTool
from supportagent.adapters.tools.logs import LogsTool
from supportagent.adapters.tools.loki import LokiSearchTool
from supportagent.adapters.tools.mcp import McpBoundTool, McpHttpClient, verify_server
from supportagent.adapters.tools.oncall import OnCallTool
from supportagent.adapters.tools.postgres import POSTGRES_TOOLS, PsycopgReader
from supportagent.adapters.tools.prometheus import PrometheusQueryTool
from supportagent.app.approvals import ApprovalService
from supportagent.app.classify import Classifier
from supportagent.app.comms import CadenceJob, CommsPlanner, CommsPlans
from supportagent.app.doctor import Bundle, Doctor, DoctorContext, DoctorReport, default_checks
from supportagent.app.extend import Scaffold, runbook_from_doc, scenario_from_incident
from supportagent.app.feedback import FeedbackService
from supportagent.app.gate import GateService
from supportagent.app.graph import ExtractionAnswer, GraphBuild, build_graph
from supportagent.app.graph import vocabulary as graph_vocabulary
from supportagent.app.grouping import GroupingService
from supportagent.app.handlers import build_handlers
from supportagent.app.ingest import HistoryExport, IngestService, SourceReport
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.memory import CaseMemory
from supportagent.app.outbox import OutboxExecutor, OutboxWriter
from supportagent.app.queries import AdminQueries
from supportagent.app.rca import (
    Budget,
    BufferedEvents,
    DocPolicy,
    Entailment,
    Knowledge,
    RcaCase,
    RcaJob,
    RcaService,
    entail_variables,
    lite_route_ids,
)
from supportagent.app.replay import ReplayService, ReplayView
from supportagent.app.retention import RetentionService
from supportagent.app.setup import SetupService, expand_grant_requests
from supportagent.app.shadow import ShadowInbound, ShadowRunner, import_history
from supportagent.app.spend import SpendBreaker
from supportagent.app.stats import StatsService
from supportagent.app.sync import SyncService
from supportagent.app.tools import ToolRegistry
from supportagent.app.triage import TriageRunner, TriageService
from supportagent.app.webhooks import WebhookDrainer, WebhookService, WebhookSource
from supportagent.config.loader import (
    load_config_default,
    load_config_offline,
    load_fault_injection_default,
    load_master_key_default,
    load_system_manifest,
    load_system_manifest_default,
)
from supportagent.config.schema import (
    AppConfig,
    ConfluenceSource,
    GitMarkdownSource,
    LLMSlot,
    RoutingRow,
    Tools,
)
from supportagent.config.schema import BasicAuth as BasicAuthConfig
from supportagent.config.system_manifest import ConfluenceSpace, GitSource
from supportagent.domain.auto import AutoConditions
from supportagent.domain.connector import ConnectorManifest, TicketSemantics, limits_for
from supportagent.domain.deprecation import deprecation_notices
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
    LLMUnavailable,
)
from supportagent.domain.faults import LLM_FAULTS, SINK_FAULTS, Fault
from supportagent.domain.knowledge import DocChunk, KnowledgeIndex, chunk_document
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.knowledge import DocFrontMatter
from supportagent.domain.model.preclass import RuleSet
from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.model.shadow import ShadowRecord
from supportagent.domain.model.triage import TriageDecision
from supportagent.domain.normalize import normalize
from supportagent.domain.onboarding import IngestedFiles, IngestedPage, TicketHistory
from supportagent.domain.packs import Pack, PackScope, PackSet, labels_text
from supportagent.domain.plugins import (
    Detection,
    DocPage,
    IncidentText,
    SourceSnapshot,
    detect,
    detected_ids,
)
from supportagent.domain.promotion import PromotionGate
from supportagent.domain.quality import DEFAULT_GATES, QualityGate, SplitCase, split_violations
from supportagent.domain.severity import SeverityStrategy, SourcePrioritySeverity
from supportagent.domain.slots import SLOT_VARIABLES, SlotName
from supportagent.domain.sop import step_scope
from supportagent.domain.trace import TraceContext
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.worker.jobs import (
    COMMS_CADENCE_INTERVAL_S,
    COMMS_CADENCE_LEASE,
    GROUPS_CLOSE_INTERVAL_S,
    GROUPS_CLOSE_LEASE,
    LLM_DEPRECATION_INTERVAL_S,
    LLM_DEPRECATION_LEASE,
    OUTBOX_EXECUTE_INTERVAL_S,
    OUTBOX_EXECUTE_LEASE,
    PROPOSALS_EXPIRE_INTERVAL_S,
    PROPOSALS_EXPIRE_LEASE,
    RCA_RUN_INTERVAL_S,
    RCA_RUN_LEASE,
    RETENTION_SWEEP_INTERVAL_S,
    RETENTION_SWEEP_LEASE,
    WEBHOOKS_DRAIN_INTERVAL_S,
    WEBHOOKS_DRAIN_LEASE,
    JobResult,
    comms_cadence,
    groups_close,
    llm_deprecation_check,
    outbox_execute,
    proposals_expire,
    rca_run,
    retention_sweep,
    sync_poll,
    sync_poll_lease,
    webhooks_drain,
)
from supportagent.entrypoints.worker.loop import ScheduledJob, Worker
from supportagent.evals.cases import EvalCase, FixtureSpec, parse_cases
from supportagent.evals.entail import EntailCase, EntailGates, EntailSuite
from supportagent.evals.entail import Verdict as EntailVerdict
from supportagent.evals.harness import Manifest
from supportagent.evals.lab import (
    LabGates,
    LabOutcome,
    LabScenario,
    WorldTools,
    cited_tools,
    load_scenarios,
)
from supportagent.evals.onboarding import (
    Inventory,
    OnboardingGates,
    OnboardingResult,
    ReferenceSystem,
    ResultEdge,
    ResultNode,
    Truth,
)
from supportagent.evals.quality import RecallReport
from supportagent.evals.scoring import CaseRun
from supportagent.ports.clock import Clock
from supportagent.ports.diagnostics import AutomationActorStore, DiscoveryProbe
from supportagent.ports.events import EventSink
from supportagent.ports.identity import IdentityProvider
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse
from supportagent.ports.tickets import RecordType, TicketSink, TicketSource
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
    """The pre-classifier rules of every pack (ARCH §4.2, 10-wp WP-010; ADR-0011).

    An empty rule set would make every case abstain, so the cross-check never
    runs and every P1/P2 is capped — the shipped rules are always loaded. Per
    case, the classifier narrows them to the service's packs (``_pack_scope``).
    """
    return _pack_set(config).rule_set()


def _pack_set(config: AppConfig) -> PackSet:
    """ADR-0011: ``common`` from the legacy paths plus every manifest under ``paths.packs``."""
    paths = config.paths
    return load_pack_set(
        taxonomy=Path(paths.taxonomy),
        hypotheses=Path(paths.hypotheses),
        runbooks=Path(paths.runbooks),
        packs_dir=Path(paths.packs) if paths.packs else None,
    )


def _pack_scope(config: AppConfig) -> PackScope:
    """The per-case pack view: the pack set plus each catalog service's ``stack:``."""
    catalog = _build_catalog(config)
    return PackScope(
        packs=_pack_set(config),
        stacks={entry.name: frozenset(entry.stack) for entry in catalog.services},
    )


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
            prompts=_prompts(config),
            events=events,
            ids=ids,
            clock=clock,
            slots=_classify_slots(config),
        )
    provider = config.llm.provider
    api_key = config.llm.api_key
    sdk_client: anthropic.Anthropic | openai.OpenAI | httpx.Client
    if provider == "anthropic":
        if api_key is None:
            raise ConfigError("llm.api_key is required for provider 'anthropic' (02 §1)")  # noqa: TRY003
        sdk_client = anthropic.Anthropic(api_key=api_key.get_secret_value())
    elif provider == "openai":
        if api_key is None:
            raise ConfigError("llm.api_key is required for provider 'openai' (02 §1)")  # noqa: TRY003
        # BL-042: base_url reaches any OpenAI-compatible server (LM Studio, vLLM);
        # None keeps the SDK's default endpoint.
        sdk_client = openai.OpenAI(api_key=api_key.get_secret_value(), base_url=config.llm.base_url)
    elif provider == "ollama":
        # BL-006: the air-gap path, a local server and no API key (ARCH §9).
        sdk_client = httpx.Client(base_url=config.llm.base_url or OLLAMA_DEFAULT_BASE_URL)
    else:
        raise ConfigError(  # noqa: TRY003 — fixture is test/--demo only
            f"container LLM wiring supports anthropic/openai/ollama; got '{provider}' (01 §5)"
        )
    return build_llm_client(
        provider_name=provider,
        sdk_client=sdk_client,
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=_prompts(config),
        events=events,
        ids=ids,
        clock=clock,
        slots=_classify_slots(config),
        decorate=chain,
    )


def _prompts(config: AppConfig) -> PromptRegistry:
    """The slot prompts, checked against the variables each slot supplies (BL-009)."""
    registry = PromptRegistry(root=Path(config.paths.prompts))
    registry.check(SLOT_VARIABLES)
    return registry


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
        taxonomy_labels=_taxonomy_labels(config),
        severity=_severity_strategy(config),
        packs=_pack_scope(config),
    )


def _severity_strategy(config: AppConfig) -> SeverityStrategy:
    """ADR-0009: the configured severity strategy (``source_priority`` for now)."""
    if config.severity.strategy == "source_priority":
        return SourcePrioritySeverity()
    raise ConfigError(f"unknown severity strategy: {config.severity.strategy}")  # noqa: TRY003


def _taxonomy_labels(config: AppConfig) -> str:
    """BL-043: the classify prompt's label list (every pack's), one line each."""
    return labels_text(_pack_set(config).taxonomy().labels)


def _build_triage(config: AppConfig) -> TriageService:
    """The live triage graph on one shared unit of work (10-wp WP-011; assisted mode)."""
    return _build_live_triage(config, uow=_build_uow(config), clock=SystemClock())


def _build_live_triage(  # noqa: PLR0913 — config plus the injectable ports and seams
    config: AppConfig,
    *,
    uow: SqlUnitOfWork,
    clock: Clock,
    ids: IdGenerator | None = None,
    master_key: bytes | None = None,
    llm_provider: LLMProvider | None = None,
    transport: httpx.BaseTransport | None = None,
) -> TriageService:
    """Assisted mode's triage (ADR-0008): proposals through the real gate."""
    ids = ids if ids is not None else UlidIdGenerator()
    events = _event_sink(uow, master_key, clock)
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
        classifier=_build_classifier(config, events, ids, clock, llm_provider),
        gate=gate,
        catalog=catalog,
        tenant_constants=config.redaction.tenant_constants,
        planner=CommsPlanner(
            uow=uow, ids=ids, gate=gate, plans=_comms(config).plans, tenant_id=config.tenant.id
        ),
        grouping=GroupingService(uow=uow, ids=ids, catalog=catalog, tenant_id=config.tenant.id),
        spend=SpendBreaker(uow=uow, daily_budget_tokens=config.llm.daily_budget_tokens),
        memory=_build_memory(config, uow, clock),
        paging=build_paging(config, transport=transport),
    )


def build_outbox_executor(  # noqa: PLR0913 — config plus the injectable ports and seams
    config: AppConfig,
    *,
    engine: Engine,
    clock: Clock,
    ids: IdGenerator,
    master_key: bytes | None = None,
    transport: httpx.BaseTransport | None = None,
) -> OutboxExecutor:
    """``outbox.execute`` (ADR-0008): the sink, the handlers, the BL-001 lease."""
    uow = SqlUnitOfWork(engine)
    return OutboxExecutor(
        uow=uow,
        repo=OutboxRepo(uow),
        clock=clock,
        ids=ids,
        events=_event_sink(uow, master_key, clock),
        sink=build_ticket_sink(config, clock=clock, transport=transport),
        handlers=build_handlers(build_paging(config, transport=transport)),
        lease_s=outbox_claim_lease_s(config, clock=clock),
        send_budget_s=outbox_claim_budget_s(config, clock=clock),
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
        semantics=ticket_semantics(config),
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


def connector_manifest(config: AppConfig) -> ConnectorManifest:
    """WP-M07: the configured connector's capabilities (ServiceNow is the only one)."""
    del config
    return SERVICENOW_MANIFEST


def ticket_semantics(config: AppConfig) -> ServiceNowSemantics:
    """WP-M07: the connector's lifecycle and severity mapping (BL-032 custom states)."""
    return ServiceNowSemantics.with_resolved_states(config.servicenow.resolved_states)


def require_connector_supports_mode(config: AppConfig, manifest: ConnectorManifest) -> None:
    """WP-M07 safe degradation: a connector missing a capability never fails open.

    Without ``history`` the agent can't see a human take a case over, and
    without ``marker_read`` a send can't be reconciled (INV-04), so either
    limits the agent to shadow mode. Customer comms need ``customer_notes``.
    """
    if config.mode == "shadow":
        return
    limits = limits_for(manifest)
    if not limits.autonomous:
        raise ConfigError(  # noqa: TRY003
            f"mode '{config.mode}' needs a connector with history and marker_read; "
            f"{manifest.name}: {'; '.join(limits.reasons)}"
        )
    routes = [*config.routing, *([config.default] if config.default else [])]
    if any(r.comms for r in routes) and not limits.customer_notes:
        raise ConfigError(  # noqa: TRY003
            f"comms plans need the customer_notes capability, which {manifest.name} lacks"
        )


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
    name: capability.result_schema for name, capability in CAPABILITIES.items()
}


def build_rca_knowledge(config: AppConfig, registered: Collection[str]) -> Knowledge:
    """WP-110: runbooks (every check compiled) and the probes of registered tools."""
    packs = _pack_set(config)
    taxonomy = packs.hypothesis_taxonomy()
    ids = [h.id for h in taxonomy.hypotheses]
    compiler = CelCheckCompiler()
    registered_schemas = {n: s for n, s in KNOWN_TOOL_SCHEMAS.items() if n in registered}
    runbooks = []
    probes = {}
    for pack in packs.packs:  # ADR-0011: every pack's runbooks and probes
        if pack.runbooks_dir is not None:
            runbooks.extend(
                load_runbooks(
                    Path(pack.runbooks_dir),
                    hypotheses=ids,
                    tool_schemas=KNOWN_TOOL_SCHEMAS,
                    compiler=compiler,
                )
            )
        if pack.probes_path is not None:
            probes.update(
                load_probes(
                    Path(pack.probes_path),
                    hypotheses=ids,
                    tool_schemas=registered_schemas,
                    compiler=compiler,
                )
            )
    return Knowledge(
        runbooks=runbooks,
        probes=probes,
        excludes={h.id: list(h.excludes) for h in taxonomy.hypotheses},
        packs=packs,
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
        docs=build_knowledge_index(config, transport=transport),
        doc_policy=doc_policy(config),
    )
    rows = [*config.routing, *([config.default] if config.default else [])]
    catalog = _build_catalog(config)
    events = _event_sink(uow, master_key, clock)
    # ADR-0014: SOP steps become proposals in assisted mode; in shadow they
    # stay in the artifact. Only an auto-mode grant on the step's own scope
    # covers one, and only when its conditions hold (part 2). A grant-covered
    # proposal (an auto step, BL-040's page note) is enqueued in the same UoW,
    # so the gate needs the outbox writer, like triage's.
    gate = (
        GateService(
            uow=uow,
            clock=clock,
            ids=ids,
            events=events,
            catalog=catalog,
            ttl=timedelta(hours=config.proposals.ttl_hours),
            outbox=OutboxWriter(uow=uow, repo=OutboxRepo(uow), ids=ids),
        )
        if config.mode == "assisted"
        else None
    )
    return RcaJob(
        uow=uow,
        service=service,
        catalog=catalog,
        buffer=buffer,
        events=events,
        gate=gate,
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


def _assisted_jobs(  # noqa: PLR0913 — the worker's shared ports
    config: AppConfig,
    *,
    engine: Engine,
    key: bytes,
    clock: Clock,
    ids: IdGenerator,
    lease: SqlLease,
    owner: str,
    transport: httpx.BaseTransport | None,
) -> list[ScheduledJob]:
    """ADR-0008 / BL-034: ``outbox.execute`` and ``comms.cadence``, assisted mode only.

    The ``outbox.execute`` job lease matches the claim lease (BL-002): claims
    are CAS-safe anyway, so the job lease only keeps two replicas from
    draining at once.
    """
    executor = build_outbox_executor(
        config, engine=engine, clock=clock, ids=ids, master_key=key, transport=transport
    )
    cadence = build_cadence_job(
        config,
        engine=engine,
        source=_build_servicenow_source(config, clock=clock, transport=transport),
        clock=clock,
        ids=ids,
        master_key=key,
    )
    job_lease_s = outbox_claim_lease_s(config, clock=clock)
    return [
        ScheduledJob(
            name=OUTBOX_EXECUTE_LEASE,
            interval_s=float(OUTBOX_EXECUTE_INTERVAL_S),
            run=lambda: outbox_execute(
                executor=executor, lease=lease, clock=clock, owner=owner, lease_ttl_s=job_lease_s
            ),
        ),
        ScheduledJob(
            name=COMMS_CADENCE_LEASE,
            interval_s=float(COMMS_CADENCE_INTERVAL_S),
            run=lambda: comms_cadence(
                run=lambda: len(cadence.run(clock.now())), lease=lease, owner=owner
            ),
        ),
    ]


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
    """The worker (02 §7): one ``sync.poll.<table>`` per table, plus the mode's jobs.

    Each poll feeds ``ShadowInbound``: a new record runs through the runner, a
    change to a known record writes one ``sync`` event. In **shadow** mode the
    runner is ``ShadowRunner`` and no ``TicketSink`` or ``outbox.execute`` job
    is built: nothing can reach ServiceNow except the read-only poll. In
    **assisted** mode (ADR-0008) the runner is the real triage, and
    ``outbox.execute`` and ``comms.cadence`` are scheduled.
    ``transport`` / ``llm_provider`` / ``engine`` are test seams.
    """
    engine = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    require_automation_decided(config, SqlAutomationActors(engine))
    require_connector_supports_mode(config, connector_manifest(config))
    clock = clock if clock is not None else SystemClock()
    ids = ids if ids is not None else UlidIdGenerator()
    key = master_key if master_key is not None else load_master_key_default()
    runner_uow = SqlUnitOfWork(engine)
    runner: ShadowRunner | TriageRunner
    if config.mode == "assisted":
        # ADR-0008: the real triage, so proposals go through the gate.
        runner = TriageRunner(
            _build_live_triage(
                config,
                uow=runner_uow,
                clock=clock,
                ids=ids,
                master_key=key,
                llm_provider=llm_provider,
                transport=transport,
            )
        )
    else:
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
            semantics=ticket_semantics(config),
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
        semantics=ticket_semantics(config),
        history=limits_for(connector_manifest(config)).takeover_detection,
        page_size=sn.page_size,
        overlap_s=sn.overlap_s,
    )
    lease = SqlLease(SqlUnitOfWork(engine), clock)
    owner = _worker_owner()

    def poll(table: RecordType) -> Callable[[], JobResult]:
        return lambda: sync_poll(poller=sync, table=table, lease=lease, owner=owner)

    jobs = [
        ScheduledJob(
            name=sync_poll_lease(RecordType(name)),
            interval_s=float(sn.poll_interval_s),
            run=poll(RecordType(name)),
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
    if config.mode == "assisted":
        jobs.extend(
            _assisted_jobs(
                config,
                engine=engine,
                key=key,
                clock=clock,
                ids=ids,
                lease=lease,
                owner=owner,
                transport=transport,
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
    if config.mode not in ("shadow", "assisted"):
        raise ConfigError(  # noqa: TRY003 — the config mode gate already enforces this (02 §1)
            f"v0.1 accepts modes 'shadow' and 'assisted'; got '{config.mode}' (ADR-0008)"
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
        taxonomy=_pack_set(config).taxonomy(),
        hypotheses=_pack_set(config).hypothesis_taxonomy(),
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
        backchannel_url=config.identity.backchannel_url,
    )
    return DoctorContext(
        config=config,
        load_bundle=lambda: _load_bundle(config),
        load_calibration=lambda: _build_calibration(config),
        schema=SqlSchemaProbe(engine, head=_head_revision()),
        records=ServiceNowTableProbe(
            http=_servicenow_http(config, clock=clock, transport=transport, max_tries=2)
        ),
        connector=connector_manifest(config),
        llm=_LazyModelProbe(lambda: _build_llm(config, _NullEvents(), ids, clock, llm_provider)),
        identity=oidc,
        automation=SqlAutomationActors(engine),
        clock=clock,
        discover_automation=discover_automation,
        load_packs=lambda: _pack_set(config),
        known_tools=frozenset(KNOWN_TOOL_SCHEMAS),
        mcp_servers=_mcp_probes(config, transport),
        load_knowledge=lambda: build_knowledge_index(config, transport=transport, strict=True),
    )


def run_doctor(config: AppConfig, *, discover_automation: bool = False) -> DoctorReport:
    """``supportagent doctor``: every 02 §4.1 check against the configured deployment.

    The report is saved for ``/ui/setup`` (BL-031). A database that can't be
    reached is itself a failing check, so a save that fails is only logged.
    """
    ctx = build_doctor_context(config, discover_automation=discover_automation)
    report = Doctor(default_checks(ctx.connector.name)).run(ctx)
    save_doctor_report(config, report, clock=ctx.clock)
    return report


def save_doctor_report(
    config: AppConfig, report: DoctorReport, *, clock: Clock, engine: Engine | None = None
) -> bool:
    """Store the last report per tenant (BL-031); ``False`` when the database refused."""
    try:
        engine = (
            engine if engine is not None else make_engine(config.database.url.get_secret_value())
        )
        SqlDoctorReports(engine).save(
            config.tenant.id, report.as_dict(), ran_ts=int(clock.now().timestamp())
        )
    except sa.exc.SQLAlchemyError as exc:
        _log.warning("doctor report not saved for /ui/setup: %s", type(exc).__name__)
        return False
    return True


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
    labels = _pack_set(config).taxonomy().labels
    return CalibrationTable(
        grade="smoke",
        snapshot_id="wiring",
        model_id=config.llm.slots[SlotName.CLASSIFY.value].model_id,
        labels={label.id: CalibrationLabelRow(agree=strong, abstain=strong) for label in labels},
    )


def _lab_config(config: AppConfig) -> AppConfig:
    """WP-M02: the lab runs RCA, so the RCA slots default to the classify slot's model."""
    slots = dict(config.llm.slots)
    classify = slots[SlotName.CLASSIFY.value]
    for slot in (SlotName.PROPOSE_HYPOTHESES.value, SlotName.ENTAIL.value):
        slots.setdefault(slot, classify)
    return config.model_copy(update={"llm": config.llm.model_copy(update={"slots": slots})})


class LabPipeline:
    """The incident lab (ADR-0011 §5, WP-M02): the real classifier and RCA per scenario.

    Only the tools are world-backed; the LLM is the configured one (``live``),
    a replay of recorded responses (``replay``), or a scripted provider in tests.
    Every request still passes the redactor (INV-06).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        provider: LLMProvider | None = None,
        decorate: Callable[[LLMProvider], LLMProvider] | None = None,
        tools_for: Callable[[LabScenario], WorldTools] = WorldTools.of,
    ) -> None:
        self._config = _lab_config(config)
        #: WP-M06: the scenario's tools; a test swaps in another backend (e.g. MCP).
        self._tools_for = tools_for
        self.model_id = self._config.llm.slots[SlotName.CLASSIFY.value].model_id
        self._clock = ReplayClock()
        self._ids = UlidIdGenerator()
        self._llm = _build_llm(
            self._config,
            _NullEvents(),
            self._ids,
            self._clock,
            provider,
            decorate=decorate,
            resilient=False,
        )
        self._packs = _pack_set(self._config)
        redactor = Redactor(detectors=built_in_detectors())
        self._redact: Callable[[str], str] = lambda text: redactor.redact(text).text
        #: Set by a caching decorator so N samples are N calls (``CachingLLMProvider.sample``).
        self.caching: CachingLLMProvider | None = None

    def run(self, scenario: LabScenario, sample: int) -> LabOutcome:
        if self.caching is not None:
            self.caching.sample = sample
        cfg = self._config
        service = scenario.service_entry
        incident = scenario.incident
        scope = PackScope(packs=self._packs, stacks={service.name: frozenset(service.stack)})
        received = datetime(2026, 1, 1, tzinfo=UTC)
        self._clock.set(received)
        evt = AlertEvent.model_validate(
            {
                "tenant_id": cfg.tenant.id,
                "source": "servicenow",
                "origin": incident.origin,
                "external_id": f"lab-{scenario.id}",
                "table": "incident",
                "ticket_number": f"LAB-{scenario.id}" if incident.origin == "ticket" else None,
                "received_at": received,
                "subject": incident.subject,
                "body": incident.body,
                "resource": service.resources[0] if service.resources else None,
                "alert_type": incident.alert_type,
                "error_signature": incident.error_signature,
                "severity_hint": severity_from_priority(incident.priority),
                "cmdb_ci": None,
                "raw_ref": f"lab:{scenario.id}",
            }
        )
        case = Case.model_validate(
            {
                "id": f"case-{scenario.id}",
                "tenant_id": cfg.tenant.id,
                "fingerprint": f"lab-{scenario.id}",
                "origin": incident.origin,
                "service": service.name,
            }
        )
        trace = TraceContext(cfg.tenant.id, f"tr_lab_{scenario.id}", "classify")
        classifier = Classifier(
            rule_set=scope.rule_set(service.name),
            llm=self._llm,
            calibration=_wiring_calibration(cfg),
            min_score=0.0,
            routing=_build_routing(cfg),
            severity=_severity_strategy(cfg),
            packs=scope,
        )
        decision = classifier.decide(evt, case, trace)
        classification = decision.classification
        if classification is None and decision.llm_suggestion is not None:
            classification = decision.llm_suggestion.classification
        tools = self._tools_for(scenario)
        rca_cfg = cfg.rca
        docs = KnowledgeIndex(
            [
                chunk
                for doc in scenario.docs
                for chunk in chunk_document(doc.meta(self._redact), doc.body, redact=self._redact)
            ],
            stale_after_days=cfg.knowledge.stale_after_days,
        )
        rca = RcaService(
            knowledge=build_rca_knowledge(cfg, tools.names),
            tools=tools,
            llm=self._llm,
            events=_NullEvents(),
            ids=self._ids,
            clock=self._clock,
            prior=rca_cfg.prior,
            freeze=rca_cfg.freeze,
            max_claims=rca_cfg.max_claims,
            entail_budget_s=rca_cfg.entail_budget_s,
            docs=docs,
            doc_policy=doc_policy(cfg),
        )
        rca_case = RcaCase(
            id=case.id,
            tenant_id=cfg.tenant.id,
            trace_id=trace.trace_id,
            service=service,
            classification=classification,
            severity=decision.severity.value if decision.severity is not None else None,
            origin=incident.origin,
            opened_ts=int(received.timestamp()),
            summary=f"{incident.subject}\n{incident.body}"[:1500],
        )
        artifact = rca.run(
            rca_case,
            Budget.of(
                RcaBudget(
                    max_tool_calls=rca_cfg.max_tool_calls,
                    max_tokens=rca_cfg.max_tokens,
                    max_hypotheses=rca_cfg.max_hypotheses,
                )
            ),
        )
        by_step = {e.step.step_id: e.step.hypothesis for e in artifact.evidence}
        return LabOutcome(
            scenario_id=scenario.id,
            classification=classification,
            severity=decision.severity.value if decision.severity is not None else None,
            preclass_label=decision.preclass_label,
            verdict=artifact.outcome,
            supported=by_step.get(artifact.supported_by or ""),
            tested=tuple(artifact.hypotheses),
            cited_tools=cited_tools(
                artifact.evidence, [c for cl in artifact.claims for c in cl.cites]
            ),
            tool_calls=len(tools.calls),
            tokens=int(artifact.budget.get("tokens", 0)),
            reason=artifact.reason,
            retrieved=tuple(str(g["doc"]) for g in artifact.guidance),
            sop_steps=tuple(m.key for m in artifact.sop_steps),
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
                taxonomy_labels=_taxonomy_labels(config),
                severity=_severity_strategy(config),
                packs=_pack_scope(config),
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
            taxonomy_labels=_taxonomy_labels(config),
            severity=_severity_strategy(config),
            packs=_pack_scope(config),
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
    if config.paths.packs:  # ADR-0011: a snapshot pins every pack file too
        packs_root = Path(config.paths.packs)
        for src in sorted(p for p in packs_root.rglob("*") if p.is_file()):
            rel = Path("packs") / src.relative_to(packs_root)
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            (out / rel).write_bytes(data)
            files[rel.as_posix()] = hashlib.sha256(data).hexdigest()
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


def _pack_tools(
    cfg: Tools,
    *,
    client: Callable[[str, Mapping[str, str]], httpx.Client],
    clock: Clock,
    transport: httpx.BaseTransport | None,
) -> list[Tool]:
    """WP-M04: the python-postgres pack's tools, each registered when configured."""
    tools: list[Tool] = []
    if cfg.postgres:  # WP-M04
        reader = PsycopgReader(
            {name: db.dsn.get_secret_value() for name, db in cfg.postgres.databases.items()},
            statement_timeout_ms=cfg.postgres.statement_timeout_ms,
        )
        tools.extend(tool_cls(reader=reader) for tool_cls in POSTGRES_TOOLS)
    if cfg.prometheus:
        auth = (
            {"Authorization": f"Bearer {cfg.prometheus.token.get_secret_value()}"}
            if cfg.prometheus.token
            else {}
        )
        tools.append(PrometheusQueryTool(client=client(cfg.prometheus.base_url, auth)))
    if cfg.loki:  # BL-050
        headers = (
            {"Authorization": f"Bearer {cfg.loki.token.get_secret_value()}"}
            if cfg.loki.token
            else {}
        )
        if cfg.loki.org_id:
            headers["X-Scope-OrgID"] = cfg.loki.org_id
        tools.append(LokiSearchTool(client=client(cfg.loki.base_url, headers), clock=clock))
    k8s_client: httpx.Client | None = None
    if cfg.kubernetes:
        k8s = cfg.kubernetes
        k8s_client = httpx.Client(
            base_url=k8s.api_url,
            headers={"Authorization": f"Bearer {k8s.token.get_secret_value()}"},
            transport=transport,
            timeout=10.0,
            verify=k8s.ca_file if k8s.ca_file else k8s.verify_tls,
        )
        tools.append(PodStatusTool(client=k8s_client))
    if cfg.changes:
        github_client = None
        if cfg.changes.github and cfg.github:
            headers = {"Accept": "application/vnd.github+json"}
            if cfg.github.token:
                headers["Authorization"] = f"Bearer {cfg.github.token.get_secret_value()}"
            github_client = client(cfg.github.base_url, headers)
        tools.append(
            RecentChangesTool(
                clock=clock,
                github=github_client,
                kubernetes=k8s_client if cfg.changes.kubernetes else None,
            )
        )
    return tools


def build_knowledge_index(
    config: AppConfig,
    *,
    transport: httpx.BaseTransport | None = None,
    strict: bool = False,
) -> KnowledgeIndex | None:
    """WP-M08: every configured knowledge source, redacted and chunked; ``None`` if none.

    A Git source that fails to load is a ``ConfigError``. A Confluence source
    (BL-051) that fails is too when ``strict`` (``doctor``); otherwise it is
    logged and its space stays empty, since docs are guidance and an
    unreachable wiki must not stop triage.
    """
    knowledge = config.knowledge
    if not knowledge.sources:
        return None
    redactor = Redactor(detectors=built_in_detectors())

    def redact(text: str) -> str:
        return redactor.redact(text).text

    chunks: list[DocChunk] = []
    for source in knowledge.sources:
        if isinstance(source, GitMarkdownSource):
            root = Path(source.path)
            chunks.extend(load_markdown_source(root, space=source.space, redact=redact))
            continue
        try:
            loaded = _load_confluence(source, redact, transport)
        except ConfigError:
            if strict:
                raise
            _log.warning("knowledge source %r unavailable; its space is empty", source.space)
            continue
        for page_id, reason in loaded.skipped:
            _log.warning("knowledge source %r: page %s skipped: %s", source.space, page_id, reason)
        chunks.extend(loaded.chunks)
    return KnowledgeIndex(chunks, stale_after_days=knowledge.stale_after_days)


def _load_confluence(
    source: ConfluenceSource,
    redact: Callable[[str], str],
    transport: httpx.BaseTransport | None,
) -> ConfluenceLoad:
    with httpx.Client(
        base_url=site_root(source.base_url),
        auth=httpx.BasicAuth(source.user, source.token),
        headers={"Accept": "application/json"},
        transport=transport,
    ) as client:
        return load_confluence_space(
            client,
            space=source.space,
            space_key=source.space_key,
            redact=redact,
            max_pages=source.max_pages,
            timeout_s=source.timeout_s,
        )


def doc_policy(config: AppConfig) -> DocPolicy:
    k = config.knowledge
    return DocPolicy(top_k=k.top_k, max_chars=k.max_guidance_chars, max_suggested=k.max_suggested)


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
    tools.extend(_pack_tools(cfg, client=client, clock=clock, transport=transport))
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
    tools = [t for t in tools if t.name not in cfg.bindings]  # WP-M06: a binding replaces it
    tools.extend(_mcp_bound_tools(cfg, _mcp_clients(cfg, transport)))
    redactor = Redactor(detectors=built_in_detectors())
    return ToolRegistry(
        tools, events=events, ids=ids, clock=clock, redact=lambda text: redactor.redact(text).text
    )


def _mcp_clients(cfg: Tools, transport: httpx.BaseTransport | None) -> dict[str, McpHttpClient]:
    """WP-M06: one client per registered MCP server (no request until first use)."""
    clients: dict[str, McpHttpClient] = {}
    for name, server in cfg.mcp_servers.items():
        headers = (
            {"Authorization": f"Bearer {server.token.get_secret_value()}"} if server.token else {}
        )
        http = httpx.Client(
            headers=headers,
            transport=transport,
            timeout=server.timeout_s,
            follow_redirects=False,
        )
        clients[name] = McpHttpClient(
            server=name, url=server.url, client=http, declared=frozenset(server.tools)
        )
    return clients


def _mcp_bound_tools(cfg: Tools, clients: Mapping[str, McpHttpClient]) -> list[Tool]:
    tools: list[Tool] = []
    for capability, binding in sorted(cfg.bindings.items()):
        known = CAPABILITIES.get(capability)
        if known is None:
            raise ConfigError(  # noqa: TRY003
                f"tools.bindings.{capability}: not a known capability "
                f"({', '.join(sorted(CAPABILITIES))})"
            )
        server = cfg.mcp_servers[binding.mcp]
        tools.append(
            McpBoundTool(
                known,
                client=clients[binding.mcp],
                tool=binding.tool,
                profile=binding.profile,
                fixed_args=binding.args,
                trust_class=server.trust_class,
                tier=server.tier,
            )
        )
    return tools


def _mcp_probes(
    config: AppConfig, transport: httpx.BaseTransport | None
) -> dict[str, Callable[[], Sequence[str]]]:
    """``doctor``: per MCP server, the registration problems (handshake + ``tools/list``)."""
    cfg = config.tools
    clients = _mcp_clients(cfg, transport)

    def probe(name: str) -> Callable[[], Sequence[str]]:
        server = cfg.mcp_servers[name]
        return lambda: verify_server(clients[name], server.trust_class, timeout_s=server.timeout_s)

    return {name: probe(name) for name in clients}


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


#: Where the ``common`` pack's lab scenarios live (other packs: their ``evals:``).
COMMON_LAB_DIR = Path("evals/lab/common")


class _UnrecordedProvider:
    """Replay mode: every request must already be recorded (``evals`` recordings)."""

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        raise LLMUnavailable("not recorded: re-run the lab with --live to record it")  # noqa: TRY003


def lab_evals_dir(config: AppConfig, pack_id: str) -> Path:
    """The scenario directory of ``pack_id`` (WP-M02)."""
    if pack_id == "common":
        return COMMON_LAB_DIR
    pack = _pack_set(config).get(pack_id)
    if pack.evals is None:
        raise ConfigError(f"pack {pack_id!r} has no evals: directory (not evaluated)")  # noqa: TRY003
    return Path(pack.evals)


@dataclass(frozen=True, slots=True)
class PackValidation:
    """``supportagent pack validate`` (WP-M09): what blocks the pack, and what to know."""

    pack_id: str
    problems: list[str]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


#: Text the generators leave for the analyst; ``--strict`` fails while any is left.
_UNFINISHED = ("Replace me", "FILL IN")


def validate_pack(config: AppConfig, pack_dir: Path, *, strict: bool = False) -> PackValidation:
    """Everything the lab and the runtime would reject, before a PR (WP-M09).

    The pack loads with the tenant's ``common`` pack and its siblings in
    ``pack_dir``'s parent, its probes and runbooks compile against the tool
    schemas, every hypothesis has a check, and its scenarios load and cover
    every hypothesis with at least one control and one holdout.
    """
    pack_dir = pack_dir.resolve()
    pack_id = pack_dir.name
    cfg = config.model_copy(
        update={"paths": config.paths.model_copy(update={"packs": str(pack_dir.parent)})}
    )
    problems: list[str] = []
    notes: list[str] = []
    try:
        packs = _pack_set(cfg)
        pack = packs.get(pack_id)
        knowledge = build_rca_knowledge(cfg, registered=KNOWN_TOOL_SCHEMAS)
    except (ConfigError, KeyError) as exc:
        return PackValidation(pack_id, [f"does not load: {exc}"], notes)
    unbound = sorted(set(pack.tools) - set(KNOWN_TOOL_SCHEMAS))
    if unbound:
        notes.append(f"tools this build doesn't know (their checks are skipped): {unbound}")
    checked = set(knowledge.probes) | {s.hypothesis for r, _ in knowledge.runbooks for s in r.steps}
    problems += [
        f"hypothesis {h.id!r} has no probe or runbook step"
        for h in pack.hypotheses
        if h.id not in checked
    ]
    if pack.evals is None:
        problems.append("no evals: directory; an unevaluated pack can't pass the lab gate")
    else:
        problems += _scenario_problems(packs, pack, Path(pack.evals))
    leftovers = sorted(
        str(f.relative_to(pack_dir))
        for f in pack_dir.rglob("*")
        if f.is_file() and any(m in f.read_text(errors="ignore") for m in _UNFINISHED)
    )
    if leftovers:
        message = f"unfinished generator text in {leftovers}"
        (problems if strict else notes).append(message)
    return PackValidation(pack_id, problems, notes)


def _scenario_problems(packs: PackSet, pack: Pack, evals: Path) -> list[str]:
    try:
        scenarios = load_scenarios(
            evals, pack=pack.id, tool_schemas=KNOWN_TOOL_SCHEMAS, read_yaml=load_yaml_document
        )
    except ConfigError as exc:
        return [f"scenarios: {exc}"]
    scope = [p for p in packs.packs if p.always or p.id == pack.id]
    labels = {label.id for p in scope for label in p.labels}
    hypotheses = {h.id for p in scope for h in p.hypotheses}
    problems: list[str] = []
    for s in scenarios:
        bad_labels = {s.truth.classification, *s.truth.also_acceptable} - labels
        if bad_labels:
            problems.append(f"scenario {s.id}: unknown label(s) {sorted(bad_labels)}")
        claimed = [s.truth.root_cause] if s.truth.root_cause is not None else []
        named = {*claimed, *s.truth.must_not_claim} - hypotheses
        if named:
            problems.append(f"scenario {s.id}: unknown hypothesis id(s) {sorted(named)}")
    causes = {s.truth.root_cause for s in scenarios}
    uncovered = sorted({h.id for h in pack.hypotheses} - causes)
    if uncovered:
        problems.append(f"no scenario has these as its root cause: {uncovered}")
    if None not in causes:
        problems.append("no control scenario (root_cause: null): show it can claim nothing")
    if not any(s.split == "holdout" for s in scenarios):
        problems.append("no holdout scenario: tuning can't be checked against unseen cases")
    return problems


def write_scaffold(scaffold: Scaffold, root: Path) -> list[Path]:
    """Write a generator's files under ``root``; refuses to overwrite any (WP-M09)."""
    targets = [(root / rel, text) for rel, text in scaffold.files.items()]
    existing = [str(path) for path, _ in targets if path.exists()]
    if existing:
        raise ConfigError(f"refusing to overwrite: {', '.join(existing)}")  # noqa: TRY003
    for path, text in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return [path for path, _ in targets]


def distill_runbook(
    config: AppConfig,
    pack_dir: Path,
    doc_file: Path,
    *,
    classification: str,
    slug: str | None = None,
) -> Scaffold:
    """``runbook new --doc`` (WP-M08): a Git doc's front-matter hypotheses as a draft runbook."""
    pack_dir = pack_dir.resolve()
    cfg = config.model_copy(
        update={"paths": config.paths.model_copy(update={"packs": str(pack_dir.parent)})}
    )
    try:
        packs = _pack_set(cfg)
        pack = packs.get(pack_dir.name)
    except KeyError:
        raise ConfigError(f"unknown pack {pack_dir.name!r}") from None  # noqa: TRY003
    knowledge = build_rca_knowledge(cfg, registered=KNOWN_TOOL_SCHEMAS)
    raw = doc_file.read_text(encoding="utf-8")
    front, _ = split_front_matter(raw, doc_file)
    front.setdefault("title", doc_file.stem)
    try:
        matter = DocFrontMatter.model_validate(front)
    except ValidationError as exc:
        raise ConfigError(f"{doc_file}: front matter: {exc}") from exc  # noqa: TRY003
    own = {h.id for h in pack.hypotheses}
    version = matter.version or hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return runbook_from_doc(
        pack_id=pack.id,
        slug=slug or doc_file.stem.lower().replace("_", "-")[:40],
        classification=classification,
        doc_ref=f"{doc_file.name} ({matter.title}, version {version})",
        hypotheses=[h for h in matter.hypotheses if h in own],
        probes={h: probe for h, (probe, _) in knowledge.probes.items() if h in own},
        labels={label.id for label in pack.labels},
        taken={r.applies_when.classification: r.id for r, _ in knowledge.runbooks},
    )


def incident_scenario(  # noqa: PLR0913 — the CLI's options
    config: AppConfig,
    pack_id: str,
    incident_file: Path,
    *,
    scrub: Sequence[str] = (),
    service: str | None = None,
    split: str = "holdout",
) -> Scaffold:
    """``scenario new --from-incident``: the file's incident, redacted (INV-06), as a scenario."""
    raw = load_yaml_document(incident_file)
    row = raw.get("result", raw) if isinstance(raw, dict) else None
    if isinstance(row, list):
        row = row[0] if row else None
    if not isinstance(row, dict):
        raise ConfigError(f"{incident_file}: expected one incident record")  # noqa: TRY003
    try:
        pack = _pack_set(config).get(pack_id)
    except KeyError:
        raise ConfigError(f"unknown pack {pack_id!r}") from None  # noqa: TRY003
    entry: dict[str, object] | None = None
    if service is not None:
        found = _build_catalog(config).service(service)
        if found is None:
            raise ConfigError(f"service {service!r} is not in the catalog")  # noqa: TRY003
        entry = found.model_dump(mode="json", exclude_none=True)
    redactor = Redactor(detectors=built_in_detectors())
    return scenario_from_incident(
        pack_id,
        row,
        redact=lambda text: redactor.redact(text).text,
        scrub=[*scrub, *config.redaction.tenant_constants],
        service=entry,
        tools=[t for t in pack.tools if t in KNOWN_TOOL_SCHEMAS],
        split=split,
    )


def load_lab(config: AppConfig, pack_id: str) -> tuple[list[LabScenario], LabGates, Path]:
    """The pack's scenarios (world validated against the tool schemas) and its gates."""
    directory = lab_evals_dir(config, pack_id)
    scenarios = load_scenarios(
        directory, pack=pack_id, tool_schemas=KNOWN_TOOL_SCHEMAS, read_yaml=load_yaml_document
    )
    raw = load_yaml_document(Path(config.paths.gates)).get("lab") or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config.paths.gates}: 'lab' must be a mapping")  # noqa: TRY003
    merged = {**(raw.get("default") or {}), **(raw.get(pack_id) or {})}
    try:
        gates = LabGates.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"{config.paths.gates}: lab gates: {exc}") from exc  # noqa: TRY003
    return scenarios, gates, directory


def build_lab_pipeline(
    config: AppConfig, *, recordings: Path, live: bool, pack_id: str
) -> LabPipeline:
    """``live`` calls the configured model and records; otherwise replay only (WP-M02)."""
    holder: list[CachingLLMProvider] = []

    def cache(inner: LLMProvider) -> LLMProvider:
        caching = CachingLLMProvider(inner, directory=recordings, snapshot_id=f"lab-{pack_id}")
        holder.append(caching)
        return caching

    pipeline = LabPipeline(config, provider=None if live else _UnrecordedProvider(), decorate=cache)
    pipeline.caching = holder[0] if holder else None
    return pipeline


#: BL-039: the citation-faithfulness suite and its recorded responses.
ENTAIL_DIR = Path("evals/entail")


class EntailPipeline:
    """BL-039: each curated pair through the ``entail`` slot, exactly as RCA sends it."""

    def __init__(
        self,
        config: AppConfig,
        *,
        provider: LLMProvider | None = None,
        decorate: Callable[[LLMProvider], LLMProvider] | None = None,
    ) -> None:
        self._config = _lab_config(config)
        self.model_id = self._config.llm.slots[SlotName.ENTAIL.value].model_id
        self._clock = ReplayClock()
        self._clock.set(datetime(2026, 1, 1, tzinfo=UTC))
        self._llm = _build_llm(
            self._config,
            _NullEvents(),
            UlidIdGenerator(),
            self._clock,
            provider,
            decorate=decorate,
            resilient=False,
        )
        self.caching: CachingLLMProvider | None = None

    def judge(self, case: EntailCase, sample: int) -> EntailVerdict | None:
        if self.caching is not None:
            self.caching.sample = sample
        trace = TraceContext(self._config.tenant.id, f"entail-{case.id}", "entail")
        got = self._llm.structured(
            SlotName.ENTAIL, entail_variables(case.claim, case.evidence), Entailment, trace
        )
        return got.verdict if got is not None else None


def load_entail_suite(config: AppConfig) -> tuple[list[EntailCase], EntailGates]:
    """BL-039: the curated pairs (``evals/entail/cases.yaml``) and the ``entail`` gates."""
    try:
        suite = EntailSuite.model_validate(load_yaml_document(ENTAIL_DIR / "cases.yaml"))
        raw = load_yaml_document(Path(config.paths.gates)).get("entail") or {}
        gates = EntailGates.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"entail suite: {exc}") from exc  # noqa: TRY003
    return list(suite.cases), gates


def build_entail_pipeline(config: AppConfig, *, live: bool) -> EntailPipeline:
    """``live`` calls the configured model and records; otherwise replay only."""
    holder: list[CachingLLMProvider] = []

    def cache(inner: LLMProvider) -> LLMProvider:
        caching = CachingLLMProvider(
            inner, directory=ENTAIL_DIR / "recordings", snapshot_id="entail"
        )
        holder.append(caching)
        return caching

    pipeline = EntailPipeline(
        config, provider=None if live else _UnrecordedProvider(), decorate=cache
    )
    pipeline.caching = holder[0] if holder else None
    return pipeline


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
    semantics: TicketSemantics | None = None,
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
            return ReplayService(repo=events, tenant_id=tenant_id).replay(trace_id)

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
        return LifecycleService(
            uow=uow,
            clock=clock,
            ids=ids,
            events=events,
            tenant_id=tenant_id,
            semantics=semantics or ServiceNowSemantics(),
        )

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
        backchannel_url=config.identity.backchannel_url,
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
        semantics=ticket_semantics(config),
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


@dataclass(frozen=True, slots=True)
class SopStepListing:
    """One SOP step as ``sop list`` shows it, with the scope an auto grant names."""

    scope: str
    doc_id: str
    title: str
    version: str | None
    when: tuple[str, ...]
    step_id: str
    kind: str
    note: str
    stale: bool


def list_sop_steps(
    config: AppConfig, *, transport: httpx.BaseTransport | None = None
) -> list[SopStepListing]:
    """``sop list``: every SOP step in the configured knowledge sources (ADR-0014)."""
    index = build_knowledge_index(config, transport=transport, strict=True)
    if index is None:
        return []
    today = SystemClock().now().date()
    return [
        SopStepListing(
            scope=step_scope(meta.doc_id, sop.digest, step),
            doc_id=meta.doc_id,
            title=meta.title,
            version=meta.version,
            when=sop.when,
            step_id=step.id,
            kind=step.kind.value,
            note=step.note,
            stale=stale,
        )
        for meta, sop, stale in index.sop_docs(today=today)
        for step in sop.steps
    ]


def request_sop_auto(  # noqa: PLR0913 — config, the step, its conditions and test seams
    config: AppConfig,
    scope: str,
    conditions: AutoConditions,
    *,
    master_key: bytes | None = None,
    transport: httpx.BaseTransport | None = None,
    engine: Engine | None = None,
) -> str:
    """``sop auto``: request an auto grant for a current, fresh SOP step (ADR-0014 part 2).

    The scope must name a step of an SOP as it is now: an edited SOP has a new
    hash, so a scope copied from an older listing is refused.
    """
    current = {s.scope: s for s in list_sop_steps(config, transport=transport)}
    step = current.get(scope)
    if step is None:
        raise ConfigError(  # noqa: TRY003
            f"{scope!r} is not a step of a current SOP; run `supportagent sop list`"
        )
    if step.stale:
        raise ConfigError(f"{step.doc_id} is stale; review the SOP first")  # noqa: TRY003
    key = master_key if master_key is not None else load_master_key_default()
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(db)
        clock = SystemClock()
        aead = Aead(master_key=key, keys=SqlKeyStore(uow), clock=clock)
        setup = SetupService(
            uow=uow,
            clock=clock,
            ids=UlidIdGenerator(),
            events=SqlEventSink(uow=uow, cipher=aead),
            catalog=_build_catalog(config),
            tenant_id=config.tenant.id,
        )
        return setup.request_sop_auto(scope, conditions, requested_by="cli:sop-auto")
    finally:
        if own:
            db.dispose()


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
            return ReplayService(repo=repo, tenant_id=config.tenant.id).replay(trace_id)
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


def demo_servicenow_app(*, interval_s: float, first_after_s: float) -> ASGIApp:
    """``supportagent demo-servicenow``: the local install's demo instance (an ASGI app)."""
    sn, _ = _build_demo_servicenow(
        clock=SystemClock(), interval_s=interval_s, first_after_s=first_after_s
    )
    return sn.app


# --- local install probes (``supportagent init``) -----------------------------
# Each check is one read-only request; a failure is a ConfigError whose message
# says what to fix, so the wizard can ask again.


def probe_llm_models(
    kind: str,
    *,
    base_url: str | None,
    api_key: str | None,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    """The model ids an LLM provider offers with these credentials."""
    try:
        return _probes.list_models(kind, base_url=base_url, api_key=api_key, transport=transport)
    except _probes.ProbeError as exc:
        raise ConfigError(str(exc)) from exc


def probe_servicenow(  # noqa: PLR0913 — basic or OAuth credentials, plus the test seam
    instance_url: str,
    *,
    integration_user: str,
    user: str | None = None,
    password: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[tuple[str, str, int]]:
    """Check the credentials and integration user; ``(ci name, sys_id, incidents)``."""
    try:
        headers = _probes.servicenow_auth(
            instance_url,
            user=user,
            password=password,
            client_id=client_id,
            client_secret=client_secret,
            transport=transport,
        )
        cis = _probes.servicenow_cis(
            instance_url,
            headers=headers,
            integration_user=integration_user,
            transport=transport,
        )
    except _probes.ProbeError as exc:
        raise ConfigError(str(exc)) from exc
    return [(c.name, c.sys_id, c.incidents) for c in cis]


def probe_confluence(
    base_url: str,
    *,
    space_key: str,
    user: str,
    token: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """The Confluence space's name, read with these credentials."""
    try:
        return _probes.confluence_space(
            base_url, space_key=space_key, user=user, token=token, transport=transport
        )
    except _probes.ProbeError as exc:
        raise ConfigError(str(exc)) from exc


# --- the onboarding lab (ADR-0015, WP-O7) --------------------------------------------

ONBOARDING_DIR = _REPO_ROOT / "evals" / "onboarding"


def load_reference_system(path: Path) -> ReferenceSystem:
    """One reference system: the manifest, the truth, and an inventory of its sources."""
    try:
        manifest = load_yaml_document(path / "system.yaml")
        truth = Truth.model_validate(
            {
                **load_yaml_document(path / "truth.yaml"),
                **load_yaml_document(path / "truth_incidents.yaml"),
            }
        )
    except ValidationError as exc:
        raise ConfigError(f"reference system {path.name}: {exc}") from exc  # noqa: TRY003
    sources = path / "sources"
    pages: set[str] = set()
    for page in sorted((sources / "confluence").rglob("*.md")):
        meta = load_front_matter(page)
        if meta.get("page_id"):
            pages.add(str(meta["page_id"]))
    git_root = sources / "git"
    files = {p.relative_to(git_root).as_posix() for p in git_root.rglob("*") if p.is_file()}
    cmdb = load_yaml_document(sources / "servicenow" / "cmdb.yaml")
    cmdb_ids = {str(r["sys_id"]) for r in [*cmdb.get("cis", []), *cmdb.get("relationships", [])]}
    live = load_yaml_document(sources / "live" / "discovery.yaml")
    records = load_yaml_records(sources / "servicenow" / "incidents.yaml")
    incidents = {str(i["number"]): str(i["priority"]) for i in records}
    system = manifest.get("system") or {}
    return ReferenceSystem(
        id=str(system.get("id", path.name)),
        name=str(system.get("name", path.name)),
        manifest=manifest,
        truth=truth,
        inventory=Inventory(
            confluence=frozenset(pages),
            git=frozenset(files),
            cmdb=frozenset(cmdb_ids),
            live=frozenset(str(k) for k in live),
            incidents=incidents,
        ),
    )


def load_reference_systems(root: Path = ONBOARDING_DIR) -> list[ReferenceSystem]:
    """Every reference system under ``evals/onboarding`` (a directory with a ``truth.yaml``)."""
    return [load_reference_system(d) for d in reference_system_dirs(root)]


def load_onboarding_gates(root: Path = ONBOARDING_DIR) -> OnboardingGates:
    try:
        return OnboardingGates.model_validate(load_yaml_document(root / "gates.yaml"))
    except ValidationError as exc:
        raise ConfigError(f"onboarding gates: {exc}") from exc  # noqa: TRY003


# --- plugins (ADR-0015 §2, WP-O1) -----------------------------------------------------

# A source checkout keeps plugins/ at the repo root; the image mounts it under
# its working directory (docker/local, docker/staging), as with migrations.
PLUGINS_DIR = next(
    (root / "plugins" for root in (_REPO_ROOT, Path.cwd()) if (root / "plugins").is_dir()),
    _REPO_ROOT / "plugins",
)


def load_plugins(root: Path = PLUGINS_DIR, *, packs_dir: Path | None = None) -> list[LoadedPlugin]:
    """The pinned plugin registry; each plugin's pack, if any, must exist."""
    plugins = load_registry(root)
    packs = packs_dir if packs_dir is not None else _REPO_ROOT / "packs"
    for plugin in plugins:
        pack = plugin.manifest.provides.pack
        if pack is not None and not (packs / pack / "pack.yaml").is_file():
            raise ConfigError(  # noqa: TRY003
                f"plugin {plugin.manifest.id}: pack {pack!r} is not under {packs}"
            )
    return plugins


def snapshot_from_sources(path: Path) -> SourceSnapshot:
    """A system's sources, in the onboarding lab's layout, as a detection snapshot.

    WP-O2 builds the same snapshot from a live system's ServiceNow, Confluence
    and Git; until then the lab's reference systems are the input.
    """
    sources = path / "sources"
    git_root = sources / "git"
    files = {
        p.relative_to(git_root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(git_root.rglob("*"))
        if p.is_file()
    }
    docs = []
    for page in sorted((sources / "confluence").rglob("*.md")):
        meta = load_front_matter(page)
        body = page.read_text(encoding="utf-8").split("---", 2)[-1]
        labels = meta.get("labels") or []
        docs.append(
            DocPage(
                page_id=str(meta.get("page_id", page.stem)),
                title=str(meta.get("title", page.stem)),
                labels=frozenset(str(label) for label in labels),
                text=body,
            )
        )
    incidents = [
        IncidentText(
            number=str(r["number"]),
            text=" ".join(
                str(r.get(k) or "") for k in ("short_description", "description", "close_notes")
            ),
            caused_by_change=bool(r.get("caused_by")),
        )
        for r in load_yaml_records(sources / "servicenow" / "incidents.yaml")
    ]
    cmdb = load_yaml_document(sources / "servicenow" / "cmdb.yaml")
    classes = frozenset(
        str(ci["sys_class_name"])
        for ci in cmdb.get("cis", [])
        if str(ci.get("install_status", "")).lower() != "retired"
    )
    manifest = load_yaml_document(path / "system.yaml")
    repos = (manifest.get("sources") or {}).get("git") or []
    hosts = frozenset(str(r["repo"]).split(":", 1)[0] for r in repos if ":" in str(r.get("repo")))
    return SourceSnapshot(
        files=files, docs=docs, incidents=incidents, cmdb_classes=classes, git_hosts=hosts
    )


def detect_plugins(path: Path, plugins: Sequence[LoadedPlugin] | None = None) -> list[Detection]:
    """Which plugins the system at ``path`` needs, with the evidence for each."""
    registry = plugins if plugins is not None else load_plugins()
    return detect([p.manifest for p in registry], snapshot_from_sources(path))


def reference_system_dirs(root: Path = ONBOARDING_DIR) -> list[Path]:
    return sorted(d for d in root.iterdir() if (d / "truth.yaml").is_file())


def _onboarding_config(config: AppConfig) -> AppConfig:
    """The config onboarding's LLM calls run with (ADR-0015 decision 3).

    With ``llm.onboarding`` set, the frontier model serves ``extract_graph``;
    otherwise the slot falls back to the triage model (``classify``).
    """
    llm = config.llm
    slots = dict(llm.slots)
    frontier = llm.onboarding
    if frontier is None:
        slots.setdefault(SlotName.EXTRACT_GRAPH.value, slots[SlotName.CLASSIFY.value])
        return config.model_copy(update={"llm": llm.model_copy(update={"slots": slots})})
    slots[SlotName.EXTRACT_GRAPH.value] = LLMSlot(
        model_id=frontier.model_id,
        timeout_s=frontier.timeout_s,
        max_output_tokens=frontier.max_output_tokens,
    )
    swapped = llm.model_copy(
        update={
            "provider": frontier.provider,
            "api_key": frontier.api_key,
            "base_url": frontier.base_url,
            "slots": slots,
        }
    )
    return config.model_copy(update={"llm": swapped})


ONBOARDING_RECORDINGS = ONBOARDING_DIR / "recordings"


class GraphPipeline:
    """WP-O3: the ``extract_graph`` slot over one documentation page (redacted, INV-06)."""

    def __init__(
        self,
        config: AppConfig,
        *,
        provider: LLMProvider | None = None,
        decorate: Callable[[LLMProvider], LLMProvider] | None = None,
    ) -> None:
        self._config = _onboarding_config(config)
        self.model_id = self._config.llm.slots[SlotName.EXTRACT_GRAPH.value].model_id
        clock = ReplayClock()
        clock.set(datetime(2026, 1, 1, tzinfo=UTC))
        self._llm = _build_llm(
            self._config,
            _NullEvents(),
            UlidIdGenerator(),
            clock,
            provider,
            decorate=decorate,
            resilient=False,
        )
        self.caching: CachingLLMProvider | None = None

    def extract(self, title: str, text: str, known: str) -> ExtractionAnswer | None:
        trace = TraceContext(self._config.tenant.id, f"graph-{abs(hash(title)) % 10**8}", "graph")
        variables = {
            "vocabulary": graph_vocabulary(),
            "known_entities": known,
            "page_title": title,
            "page_text": text,
        }
        return self._llm.structured(SlotName.EXTRACT_GRAPH, variables, ExtractionAnswer, trace)


def build_graph_pipeline(config: AppConfig, *, live: bool) -> GraphPipeline:
    """``live`` calls the model and records; otherwise replay ``evals/onboarding/recordings``."""
    holder: list[CachingLLMProvider] = []

    def cache(inner: LLMProvider) -> LLMProvider:
        caching = CachingLLMProvider(inner, directory=ONBOARDING_RECORDINGS, snapshot_id="graph")
        holder.append(caching)
        return caching

    pipeline = GraphPipeline(
        config, provider=None if live else _UnrecordedProvider(), decorate=cache
    )
    pipeline.caching = holder[0] if holder else None
    return pipeline


def build_system_graph(path: Path, pipeline: GraphPipeline | None) -> GraphBuild:
    """The system graph of a workspace (or a reference system): structured, then prose."""
    snapshot = snapshot_from_sources(path)
    sources = path / "sources"
    cmdb = load_yaml_document(sources / "servicenow" / "cmdb.yaml")
    live_path = sources / "live" / "discovery.yaml"
    live = load_yaml_document(live_path) if live_path.is_file() else {}
    facts = structured_facts(snapshot.files, cmdb, live)
    return build_graph(facts, snapshot.docs, pipeline.extract if pipeline is not None else None)


def run_onboarding_pipeline(
    path: Path,
    plugins: Sequence[LoadedPlugin] | None = None,
    graph: GraphPipeline | None = None,
) -> tuple[OnboardingResult, list[Detection], GraphBuild]:
    """What the onboarding stages built so far produce for the system at ``path``.

    WP-O1: plugin detection. WP-O3: the system graph (prose only with a
    ``graph`` pipeline). The incident map and requests join as their WPs land.
    """
    detections = detect_plugins(path, plugins)
    built = build_system_graph(path, graph)
    result = OnboardingResult(
        plugins=detected_ids(detections),
        nodes=[ResultNode(id=n.id, kind=n.kind) for n in built.graph.nodes.values()],
        edges=[ResultEdge(src=a, kind=b, dst=c) for a, b, c in built.graph.edges],
    )
    return result, detections, built


def pin_plugins(root: Path = PLUGINS_DIR) -> list[str]:
    """``supportagent plugin index``: re-pin every plugin as it is now (after review)."""
    return write_index(root)


# --- onboarding ingest (ADR-0015 P1, WP-O2) -------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestTransports:
    """Test seams: fake ServiceNow, Confluence and GitHub servers."""

    servicenow: httpx.BaseTransport | None = None
    confluence: httpx.BaseTransport | None = None
    github: httpx.BaseTransport | None = None


def ingest_system(  # noqa: PLR0913 — the config, the manifest, the workspace and the seams
    config: AppConfig | None,
    manifest_path: Path,
    out_dir: Path,
    *,
    refresh: bool = False,
    transports: IngestTransports = IngestTransports(),  # noqa: B008 — frozen, immutable
    env: Mapping[str, str] | None = None,
) -> list[SourceReport]:
    """``onboard ingest``: read the manifest's sources into a workspace at ``out_dir``.

    ServiceNow is read with ``config``'s connection; without a config, the
    ServiceNow source is reported unavailable and the others still run.
    """
    manifest = (
        load_system_manifest(manifest_path, env)
        if env is not None
        else load_system_manifest_default(manifest_path)
    )
    redactor = Redactor(detectors=built_in_detectors())

    def redact(text: str) -> str:
        return redactor.redact(text).text

    scope = manifest.sources.servicenow
    history: HistoryExport | None = None
    if config is not None and scope is not None:
        http = _servicenow_http(
            config, clock=SystemClock(), transport=transports.servicenow, max_tries=3
        )

        def history(*, max_incidents: int, now: datetime) -> TicketHistory:
            return export_history(
                http,
                assignment_groups=scope.assignment_groups,
                history_months=scope.history_months,
                max_incidents=max_incidents,
                now=now,
            )

    def confluence(space: ConfluenceSpace, *, max_pages: int) -> tuple[list[IngestedPage], bool]:
        if not space.base_url or space.user is None or space.token is None:
            raise ConfigError(  # noqa: TRY003
                f"Confluence space {space.space}: base_url, user_env and token_env are required"
            )
        with httpx.Client(
            base_url=site_root(space.base_url),
            auth=httpx.BasicAuth(space.user.get_secret_value(), space.token.get_secret_value()),
            headers={"Accept": "application/json"},
            transport=transports.confluence,
            timeout=20.0,
        ) as client:
            return export_confluence_space(client, space_key=space.space, max_pages=max_pages)

    def git(source: GitSource, *, max_files: int, max_file_bytes: int) -> IngestedFiles:
        found = IngestedFiles()
        spec = source.repo.split(":", 1)[1]
        if source.host == "file":
            read_local(
                spec, source.paths, max_files=max_files, max_file_bytes=max_file_bytes, into=found
            )
            return found
        token = source.token.get_secret_value() if source.token is not None else None
        reader = GitHubReader(token=token, transport=transports.github)
        try:
            reader.read(
                spec, source.paths, max_files=max_files, max_file_bytes=max_file_bytes, into=found
            )
        finally:
            reader.close()
        return found

    service = IngestService(
        workspace=Workspace(out_dir),
        redact=redact,
        now=SystemClock().now,
        history=history,
        history_label="servicenow" if scope is not None else None,
        confluence=confluence,
        git=git,
    )
    return service.run(manifest, manifest_path.read_text(encoding="utf-8"), refresh=refresh)


@dataclass(frozen=True, slots=True)
class OnboardingLLM:
    """Which model onboarding uses (ADR-0015 decision 3) and why."""

    provider: str
    model_id: str
    base_url: str | None
    #: ``onboarding``: the configured frontier model; ``triage``: the classify slot's.
    source: Literal["onboarding", "triage"]


def onboarding_llm(config: AppConfig) -> OnboardingLLM:
    """The frontier model when ``llm.onboarding`` is set, else the triage model."""
    chosen = config.llm.onboarding
    if chosen is not None:
        return OnboardingLLM(chosen.provider, chosen.model_id, chosen.base_url, "onboarding")
    classify = config.llm.slots["classify"]
    return OnboardingLLM(config.llm.provider, classify.model_id, config.llm.base_url, "triage")
