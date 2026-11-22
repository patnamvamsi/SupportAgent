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

import functools
import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import tempfile
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
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
from supportagent.adapters.crypto.vault import FileCredentialVault
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
from supportagent.adapters.files import git_catalog
from supportagent.adapters.files._yaml import dump_yaml
from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.change_set import apply_change_set
from supportagent.adapters.files.comms_loader import CommsCatalog, load_comms_catalog
from supportagent.adapters.files.deprecation_loader import load_deprecation_feed
from supportagent.adapters.files.eval_loader import (
    load_eval_documents,
    load_front_matter,
    load_yaml_document,
    load_yaml_records,
)
from supportagent.adapters.files.gates_loader import load_promotion_gate, load_quality_gates
from supportagent.adapters.files.graph_extract import mapped_cmdb_classes, structured_facts
from supportagent.adapters.files.knowledge_loader import load_markdown_source, split_front_matter
from supportagent.adapters.files.pack_loader import load_pack_set
from supportagent.adapters.files.plugin_loader import (
    LoadedPlugin,
    disabled_plugins,
    index_sha256,
    install_plugin,
    load_manifest,
    load_registry,
    pin_one,
    set_disabled,
    write_index,
)
from supportagent.adapters.files.runbook_loader import load_probes, load_runbooks
from supportagent.adapters.files.signal_rules import (
    read_drafts,
    read_enabled,
    write_review,
    write_rules,
)
from supportagent.adapters.files.taxonomy_loader import load_classification
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
from supportagent.adapters.llm.budget import BudgetedProvider
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
from supportagent.adapters.pii.presidio import PiiRecognizerSpec, PresidioAnalyzer
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
from supportagent.adapters.servicenow.history_export import export_history, past_incidents
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
from supportagent.adapters.tools.access import access_verifiers
from supportagent.adapters.tools.airflow import AirflowTool
from supportagent.adapters.tools.capabilities import CAPABILITIES, Capability
from supportagent.adapters.tools.changes import RecentChangesTool
from supportagent.adapters.tools.cron import CronStatusTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.kubernetes import PodStatusTool
from supportagent.adapters.tools.logs import LogsTool
from supportagent.adapters.tools.loki import SCHEMA as LOKI_SCHEMA
from supportagent.adapters.tools.loki import LokiSearchTool, render_logql, streams_result
from supportagent.adapters.tools.mcp import McpBoundTool, McpHttpClient, verify_server
from supportagent.adapters.tools.oncall import OnCallTool
from supportagent.adapters.tools.oracle import ORACLE_TOOLS, OracledbReader
from supportagent.adapters.tools.postgres import POSTGRES_TOOLS, PsycopgReader
from supportagent.adapters.tools.prometheus import PrometheusQueryTool
from supportagent.adapters.tools.records import PsycopgRecordReader, RecordsQueryTool
from supportagent.adapters.tools.signal_history import HttpSignalHistory, signal_history
from supportagent.app.access_binding import AccessBinding, BindOutcome
from supportagent.app.approvals import ApprovalService
from supportagent.app.classify import Classifier
from supportagent.app.comms import CadenceJob, CommsPlanner, CommsPlans
from supportagent.app.component_summaries import (
    ComponentSummaryStore,
    SummaryAnswer,
    SummaryRun,
    summarize_components,
)
from supportagent.app.doctor import Bundle, Doctor, DoctorContext, DoctorReport, default_checks
from supportagent.app.doctor.base import OnboardingFacts
from supportagent.app.extend import Scaffold, runbook_from_doc, scenario_from_incident
from supportagent.app.feedback import FeedbackService
from supportagent.app.gate import GateService
from supportagent.app.graph import ExtractionAnswer, GraphBuild, build_graph
from supportagent.app.graph import vocabulary as graph_vocabulary
from supportagent.app.graph_review import GraphReview
from supportagent.app.grouping import GroupingService
from supportagent.app.handlers import build_handlers
from supportagent.app.incident_map import IncidentsAnswer, map_incidents
from supportagent.app.ingest import HistoryExport, IngestService, SourceReport
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.live_proof import LiveProof, LiveRca
from supportagent.app.memory import CaseMemory
from supportagent.app.onboarding_audit import OnboardingAudit
from supportagent.app.onboarding_audit import history as audit_history
from supportagent.app.onboarding_control import (
    STALE_RUN_S,
    OnboardingControl,
    OnboardingControlStore,
)
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
from supportagent.app.readiness_store import OnboardingStatusStore, ReadinessStore
from supportagent.app.replay import ReplayService, ReplayView
from supportagent.app.request_review import RequestReview, RequestView
from supportagent.app.retention import RetentionService
from supportagent.app.setup import SetupService, expand_grant_requests
from supportagent.app.shadow import ShadowInbound, ShadowRunner, import_history
from supportagent.app.spend import SpendBreaker
from supportagent.app.stats import StatsService
from supportagent.app.sync import SyncService
from supportagent.app.tools import ToolRegistry
from supportagent.app.trace_answer import TraceAnswer, TraceExplainer
from supportagent.app.tracing import FlowTracer, TraceRun
from supportagent.app.triage import TriageRunner, TriageService
from supportagent.app.webhooks import (
    AlertResolutions,
    WebhookDrainer,
    WebhookService,
    WebhookSource,
)
from supportagent.config.loader import (
    load_config_default,
    load_config_offline,
    load_fault_injection_default,
    load_master_key_default,
    load_system_manifest,
    load_system_manifest_default,
    plugins_dir_override,
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
from supportagent.config.schema import ServiceNow as ServiceNowConfig
from supportagent.config.system_manifest import Budget as ManifestBudget
from supportagent.config.system_manifest import ConfluenceSpace, GitSource, SystemManifest
from supportagent.domain.auto import AutoConditions
from supportagent.domain.autopilot import (
    STEPS,
    Need,
    OnboardingStatus,
    State,
    Step,
    StepRecord,
    digest,
    should_run,
)
from supportagent.domain.classification import prefix_match
from supportagent.domain.connector import ConnectorManifest, TicketSemantics, limits_for
from supportagent.domain.deprecation import deprecation_notices
from supportagent.domain.drift import ClassShape, Finding, Snapshot, diff
from supportagent.domain.errors import (
    ConfigError,
    DomainValidationError,
    ExternalServiceError,
    LLMUnavailable,
    ToolError,
)
from supportagent.domain.evidence import PlannedRead, capture, capture_plan
from supportagent.domain.faults import LLM_FAULTS, SINK_FAULTS, Fault
from supportagent.domain.flow_candidates import Candidate, Ticket, candidates, draft_flow
from supportagent.domain.flow_trace import StageObservation
from supportagent.domain.gap_plan import GapPlan, target_name
from supportagent.domain.gap_plan import plan as gap_plan
from supportagent.domain.gaps import (
    Gap,
    technology,
    uncaused_incidents,
    unclaimed_repos,
    unknown_capabilities,
    unmapped_cmdb_classes,
)
from supportagent.domain.graph import SystemGraph
from supportagent.domain.ids import IdPrefix
from supportagent.domain.incident_map import Catalogue, IncidentMap, PastIncident, catalogue
from supportagent.domain.knowledge import DocChunk, KnowledgeIndex, chunk_document
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.flow import Flow
from supportagent.domain.model.knowledge import DocFrontMatter
from supportagent.domain.model.plugin import Discovery, cause_hypothesis
from supportagent.domain.model.preclass import RuleSet
from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.model.shadow import ShadowRecord
from supportagent.domain.model.triage import TriageDecision
from supportagent.domain.normalize import normalize
from supportagent.domain.onboarding import IngestedFiles, IngestedPage, TicketHistory
from supportagent.domain.packs import Pack, PackScope, PackSet, labels_text
from supportagent.domain.pii import PiiSpan
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
from supportagent.domain.readiness import ReadinessPolicy
from supportagent.domain.severity import Severity, SeverityStrategy, SourcePrioritySeverity
from supportagent.domain.signal_detectors import (
    NotProposed,
    SignalDetector,
    draft_detectors,
    with_enabled,
)
from supportagent.domain.slots import SLOT_VARIABLES, SlotName
from supportagent.domain.sop import step_scope
from supportagent.domain.tiers import Tier, TrustClass
from supportagent.domain.trace import TraceContext
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.worker.jobs import (
    COMMS_CADENCE_INTERVAL_S,
    COMMS_CADENCE_LEASE,
    GROUPS_CLOSE_INTERVAL_S,
    GROUPS_CLOSE_LEASE,
    LLM_DEPRECATION_INTERVAL_S,
    LLM_DEPRECATION_LEASE,
    ONBOARD_AUTOPILOT_INTERVAL_S,
    ONBOARD_AUTOPILOT_LEASE,
    ONBOARD_VERIFY_LEASE,
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
    AutopilotJob,
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
from supportagent.entrypoints.worker.jobs import onboard_verify as onboard_verify_job
from supportagent.entrypoints.worker.loop import ScheduledJob, Worker
from supportagent.evals.cases import EvalCase, FixtureSpec, parse_cases
from supportagent.evals.entail import EntailCase, EntailGates, EntailSuite
from supportagent.evals.entail import Verdict as EntailVerdict
from supportagent.evals.harness import Manifest
from supportagent.evals.lab import (
    LabGates,
    LabOutcome,
    LabScenario,
    WorldEntry,
    WorldTools,
    check_shape,
    cited_tools,
    load_scenarios,
    run_lab,
    score_outcome,
)
from supportagent.evals.onboarding import (
    Inventory,
    OnboardingGates,
    OnboardingResult,
    ReferenceSystem,
    ResultClass,
    ResultEdge,
    ResultNode,
    ResultRequest,
    Truth,
)
from supportagent.evals.quality import RecallReport
from supportagent.evals.readiness import (
    ReadinessReport,
    Replayed,
    ShadowCase,
    apply_shadow,
    assess,
    fit_calibration,
    history_scenarios,
    service_entry,
)
from supportagent.evals.readiness import incident_scenario as history_scenario
from supportagent.evals.scoring import CaseRun
from supportagent.evals.trace_lab import TraceGates, TraceOutcome, TraceScenario, TraceSuite
from supportagent.ports.access import AccessVerifier
from supportagent.ports.clock import Clock
from supportagent.ports.diagnostics import AutomationActorStore, DiscoveryProbe, ProbeRows
from supportagent.ports.events import EventSink
from supportagent.ports.identity import IdentityProvider
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse
from supportagent.ports.pii import PiiAnalyzer
from supportagent.ports.signals import SignalHistoryReader
from supportagent.ports.tickets import RecordType, TicketSink, TicketSource
from supportagent.ports.tools import Tool, ToolResult

# ``src/supportagent/bootstrap.py`` → repo root (the checkout the user runs).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _shipped(*parts: str) -> Path:
    """Shipped content (``packs/``, ``taxonomy/``, ``evals/``…): the source checkout's,
    else the working directory's. An installed wheel has no source tree; the image
    carries the content in its working directory, as with migrations."""
    for root in (_REPO_ROOT, Path.cwd()):
        if (root / parts[0]).exists():
            return root.joinpath(*parts)
    return _REPO_ROOT.joinpath(*parts)


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
        embedded=_embedded_packs(plugins_root(), _index_stamp(plugins_root())),
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


def _build_classifier(  # noqa: PLR0913 — the ports plus the provider and readiness seams
    config: AppConfig,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
    provider_override: LLMProvider | None = None,
    *,
    readiness: ReadinessPolicy | None = None,
    calibration: CalibrationTable | None = None,
) -> Classifier:
    """The ARCH §4 classifier: shipped pre-classifier rules, LLM, calibration, routing.

    WP-O6b: ``readiness`` (the system's last report, read at build) routes only
    the labels of ready classes; ``None`` (not onboarded) routes as before.
    """
    return Classifier(
        rule_set=_build_ruleset(config),
        llm=_build_llm(config, events, ids, clock, provider_override),
        calibration=calibration or _classifier_calibration(config),
        min_score=config.calibration.min_score,
        routing=_build_routing(config),
        taxonomy_labels=_taxonomy_labels(config),
        severity=_severity_strategy(config),
        packs=_pack_scope(config),
        readiness=readiness,
    )


def _fitted_calibration(
    config: AppConfig, uow: SqlUnitOfWork, clock: Clock
) -> CalibrationTable | None:
    """ADR-0017 (WP-A5): the calibration fitted from the system's history, if it was
    fitted with the model triage now uses; otherwise the configured artifact applies."""
    report = ReadinessStore(uow=uow, clock=clock, tenant_id=config.tenant.id).load()
    return _calibration_in_use(config, report)[0]


def _calibration_in_use(
    config: AppConfig, report: Mapping[str, object] | None
) -> tuple[CalibrationTable | None, str]:
    """The fitted table triage scores with (``None``: the configured artifact), and why."""
    if config.onboarding.calibration == "artifact":
        return None, "onboarding.calibration: artifact"  # the switch
    raw = report.get("calibration") if report else None
    if not isinstance(raw, dict):
        return None, "nothing fitted yet"
    try:
        table = CalibrationTable.model_validate(raw)
    except ValidationError:
        return None, "the fitted table doesn't parse"
    configured = config.llm.slots[SlotName.CLASSIFY.value].model_id
    if table.signature != table.computed_signature():
        return None, "the fitted table's counts were edited (signature mismatch)"
    if table.model_id != configured:
        return None, f"fitted with {table.model_id}, triage uses {configured}"
    return table, f"fitted with {table.model_id}"


def _readiness_policy(
    config: AppConfig, uow: SqlUnitOfWork, clock: Clock
) -> ReadinessPolicy | None:
    """WP-O6b: the routing policy of the system's last readiness report, if it has one."""
    if config.onboarding.routing == "report":
        return None  # the switch: readiness is measured, not enforced
    return ReadinessStore(uow=uow, clock=clock, tenant_id=config.tenant.id).policy()


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
        classifier=_build_classifier(
            config,
            events,
            ids,
            clock,
            llm_provider,
            readiness=_readiness_policy(config, uow, clock),
            calibration=_fitted_calibration(config, uow, clock),
        ),
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
        integration_user=ticket_connector(config).integration_user,
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
        classifier=_build_classifier(
            config,
            events,
            ids,
            clock,
            llm_provider,
            readiness=_readiness_policy(config, uow, clock),
            calibration=_fitted_calibration(config, uow, clock),
        ),
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


#: ADR-0022: a shadow deployment with no ticket connector (webhook alerts only).
NO_TICKET_CONNECTOR = ConnectorManifest(name="none", capabilities=frozenset())


def connector_manifest(config: AppConfig) -> ConnectorManifest:
    """WP-M07: the configured connector's capabilities (ServiceNow is the only one)."""
    return SERVICENOW_MANIFEST if config.servicenow is not None else NO_TICKET_CONNECTOR


def ticket_semantics(config: AppConfig) -> ServiceNowSemantics:
    """WP-M07: the connector's lifecycle and severity mapping (BL-032 custom states)."""
    sn = config.servicenow
    return ServiceNowSemantics.with_resolved_states(sn.resolved_states if sn else None)


def ticket_connector(config: AppConfig) -> ServiceNowConfig:
    """The configured ticket connector; ``ConfigError`` when the deployment has none."""
    if config.servicenow is None:
        raise ConfigError(  # noqa: TRY003
            "no ticket connector configured (servicenow): this needs one"
        )
    return config.servicenow


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
    sn = ticket_connector(config)
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
    name = ticket_connector(config).integration_user
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


def capability_catalog() -> Mapping[str, Capability]:
    """Every capability: the built-in tools' (WP-107) and the pinned plugins' (ADR-0016)."""
    return _catalog_of(plugins_root())


@functools.cache
def _catalog_of(root: Path) -> Mapping[str, Capability]:
    return merge_capabilities(load_plugins(root))


def merge_capabilities(plugins: Sequence[LoadedPlugin]) -> dict[str, Capability]:
    """The built-in capabilities plus each one a plugin declares a ``result_schema`` for
    (ADR-0016): an MCP server serves it, with no core code.

    A plugin can't redefine a built-in capability's schema, and two plugins that
    declare one capability must agree on it.
    """
    out: dict[str, Capability] = dict(CAPABILITIES)
    owner: dict[str, str] = {}
    for plugin in plugins:
        for name, spec in sorted(plugin.manifest.provides.capabilities.items()):
            if spec.result_schema is None:
                continue
            pid = plugin.manifest.id
            if name in CAPABILITIES:
                raise ConfigError(  # noqa: TRY003
                    f"plugin {pid}: {name} is a built-in capability; its schema is the tool's"
                )
            declared = Capability(name, spec.result_schema, frozenset(spec.literal_args))
            if name in out and out[name] != declared:
                raise ConfigError(  # noqa: TRY003
                    f"plugins {owner[name]} and {pid} declare {name} differently"
                )
            out[name], owner[name] = declared, pid
    return out


class _CatalogSchemas(Mapping[str, Mapping[str, object]]):
    """``capability_catalog``'s result schemas, read on first use (importing reads no plugin)."""

    def __getitem__(self, name: str) -> Mapping[str, object]:
        return capability_catalog()[name].result_schema

    def __iter__(self) -> Iterator[str]:
        return iter(capability_catalog())

    def __len__(self) -> int:
        return len(capability_catalog())


#: The declared result schema of every capability. Runbooks are validated
#: against these whether or not the tool is configured; an unconfigured
#: tool's step simply can't run (ARCH §5.5).
KNOWN_TOOL_SCHEMAS: Mapping[str, Mapping[str, object]] = _CatalogSchemas()


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
    tools = build_tool_registry(
        config,
        events=buffer,
        ids=ids,
        clock=clock,
        transport=transport,
        credentials=bound_credentials(config, master_key),
    )
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
        readiness=_readiness_policy(config, uow, clock),
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
        # WP-O6c: in shadow, a class that isn't ready is proven on its live incidents
        prove_live=config.mode == "shadow",
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


def _ticket_sync_jobs(  # noqa: PLR0913 — the worker's shared ports
    config: AppConfig,
    sn: ServiceNowConfig,
    *,
    engine: Engine,
    runner: ShadowRunner | TriageRunner,
    runner_uow: SqlUnitOfWork,
    key: bytes,
    clock: Clock,
    ids: IdGenerator,
    transport: httpx.BaseTransport | None,
    lease: SqlLease,
    owner: str,
) -> list[ScheduledJob]:
    """One ``sync.poll.<table>`` per ticket table, each feeding ``ShadowInbound``."""
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
            integration_user=sn.integration_user,
            classification_map=sn.classification_map,
        ),
        lifecycle=LifecycleService(
            uow=runner_uow,
            clock=clock,
            ids=ids,
            events=inbound_events,
            tenant_id=config.tenant.id,
            integration_user=sn.integration_user,
            integration_ids=_integration_ids(config, clock=clock, transport=transport),
            semantics=ticket_semantics(config),
        ),
    )
    sync_uow = SqlUnitOfWork(engine)
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

    def poll(table: RecordType) -> Callable[[], JobResult]:
        return lambda: sync_poll(poller=sync, table=table, lease=lease, owner=owner)

    return [
        ScheduledJob(
            name=sync_poll_lease(RecordType(name)),
            interval_s=float(sn.poll_interval_s),
            run=poll(RecordType(name)),
        )
        for name in sn.tables
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
    lease = SqlLease(SqlUnitOfWork(engine), clock)
    owner = _worker_owner()
    # ADR-0022: with no ticket connector, alerts arrive by webhook only.
    jobs = (
        []
        if config.servicenow is None
        else _ticket_sync_jobs(
            config,
            config.servicenow,
            engine=engine,
            runner=runner,
            runner_uow=runner_uow,
            key=key,
            clock=clock,
            ids=ids,
            transport=transport,
            lease=lease,
            owner=owner,
        )
    )
    if config.webhooks:
        inbox_uow = SqlUnitOfWork(engine)
        inbox_cipher = Aead(master_key=key, keys=SqlKeyStore(inbox_uow), clock=clock)
        resolved_uow = SqlUnitOfWork(engine)
        drainer = WebhookDrainer(
            uow=inbox_uow,
            inbox=WebhookInbox(inbox_uow, inbox_cipher),
            runner=runner,
            clock=clock,
            tenant_id=config.tenant.id,
            resolutions=AlertResolutions(
                uow=resolved_uow,
                clock=clock,
                ids=ids,
                events=SqlEventSink(
                    uow=resolved_uow,
                    cipher=Aead(master_key=key, keys=SqlKeyStore(resolved_uow), clock=clock),
                ),
                tenant_id=config.tenant.id,
                tenant_constants=config.redaction.tenant_constants,
            ),
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
    lite = any(row.rca == "lite" for row in [*config.routing, *filter(None, [config.default])])
    # ADR-0020: in shadow, RCA also proves held-back classes live, lite routes or not
    if lite or config.mode == "shadow":
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
    autopilot = AutopilotJob(
        claim=lambda: onboarding_control_for(config, engine, clock).claim(
            interval_s=config.onboarding.autopilot_interval_h * 3600
        ),
        run=lambda claim: run_autopilot(
            config, engine=engine, master_key=key, clock=clock, llm_provider=llm_provider
        ),
        lease=lease,
        owner=owner,
        ttl_s=STALE_RUN_S,
    )
    jobs.append(
        ScheduledJob(
            name=ONBOARD_AUTOPILOT_LEASE,
            interval_s=float(ONBOARD_AUTOPILOT_INTERVAL_S),
            run=autopilot.tick,
        )
    )
    if config.onboarding.verify_interval_h > 0:
        jobs.append(
            ScheduledJob(
                name=ONBOARD_VERIFY_LEASE,
                interval_s=float(config.onboarding.verify_interval_h * 3600),
                run=lambda: onboard_verify_job(
                    verify=lambda: [
                        (o.request.id, o.bound, o.why or "")
                        for o in onboard_verify(
                            config, actor="worker", engine=engine, master_key=key, clock=clock
                        )
                    ],
                    lease=lease,
                    owner=owner,
                ),
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


class _NoRecords:
    """The record probe of a deployment with no ticket connector: nothing to read."""

    def read(self, table: str, *, query: str, fields: Sequence[str], limit: int) -> ProbeRows:
        del query, fields, limit
        raise ConfigError(f"no ticket connector configured to read {table!r}")  # noqa: TRY003


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
        records=(
            ServiceNowTableProbe(
                http=_servicenow_http(config, clock=clock, transport=transport, max_tries=2)
            )
            if config.servicenow is not None
            else _NoRecords()
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
        load_onboarding=lambda: onboarding_facts(config, engine, clock),
        pii=build_pii_analyzer(config),
    )


def load_flow_file(path: Path) -> Flow:
    """One flow file (ADR-0021 §2); ``ConfigError`` naming the file when it doesn't validate."""
    try:
        return Flow.model_validate(load_yaml_document(path))
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc.errors()[0]['msg']}") from exc  # noqa: TRY003


def load_flows(config: AppConfig) -> dict[str, Flow]:
    """Every flow in ``tracing.flows_dir`` by id, reviewed or not; none when it doesn't exist."""
    root = Path(config.tracing.flows_dir)
    flows: dict[str, Flow] = {}
    for path in sorted(root.glob("*.yaml")) if root.is_dir() else []:
        flow = load_flow_file(path)
        if flow.flow != path.stem:
            raise ConfigError(f"{path}: flow {flow.flow!r} must live in {flow.flow}.yaml")  # noqa: TRY003
        flows[flow.flow] = flow
    return flows


def trace_object(  # noqa: PLR0913 — the question plus the seams tests replace
    config: AppConfig,
    flow_id: str,
    object_id: str,
    *,
    at: datetime | None = None,
    actor: str = "cli",
    engine: Engine | None = None,
    master_key: bytes | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Clock | None = None,
    pii: PiiAnalyzer | None = None,
    explain: bool = True,
    llm_provider: LLMProvider | None = None,
) -> tuple[TraceRun, TraceAnswer | None]:
    """``supportagent trace`` (ADR-0021): one object through one reviewed flow, audited.

    With ``explain``, RCA runs on the component where the object stopped and the
    model writes a cited answer over the trace's facts (``TraceExplainer``).
    """
    flow = load_flows(config).get(flow_id)
    if flow is None:
        raise ConfigError(f"no flow {flow_id!r} in {config.tracing.flows_dir}")  # noqa: TRY003
    if flow.reads_records() and flow.flow not in config.tracing.record_flows:
        raise ConfigError(  # noqa: TRY003
            f"flow {flow.flow} reads business records: an admin must list it in "
            "tracing.record_flows first (ADR-0021 §4)"
        )
    clock = clock or SystemClock()
    with _tracing(config, engine, master_key, transport, clock, pii) as (
        tracer,
        tools,
        events,
        ids,
    ):
        span = ids.new(IdPrefix.EVENT)
        context = TraceContext.new(config.tenant.id, f"trace_{span}", f"span_{span}")
        run = tracer.trace(flow, object_id, context, actor=actor, at=at)
        answer = None
        if explain:
            llm = _build_llm(config, events, ids, clock, llm_provider)
            rca = config.rca
            service = RcaService(
                knowledge=build_rca_knowledge(config, tools.names),
                tools=tools,
                llm=llm,
                events=events,
                ids=ids,
                clock=clock,
                prior=rca.prior,
                freeze=rca.freeze,
                max_claims=rca.max_claims,
                entail_budget_s=rca.entail_budget_s,
            )
            answer = TraceExplainer(
                llm=llm,
                catalog=_build_catalog(config),
                rca=service.run,
                budget=lambda: Budget.of(
                    RcaBudget(
                        max_tool_calls=rca.max_tool_calls,
                        max_tokens=rca.max_tokens,
                        max_hypotheses=rca.max_hypotheses,
                    )
                ),
            ).explain(flow, run, context, pseudonymise=run.pseudonymise)
    return run, answer


@contextmanager
def _tracing(  # noqa: PLR0913, PLR0917 — the database, its key and the seams tests replace
    config: AppConfig,
    engine: Engine | None,
    master_key: bytes | None,
    transport: httpx.BaseTransport | None,
    clock: Clock,
    pii: PiiAnalyzer | None,
) -> Iterator[tuple[FlowTracer, ToolRegistry, EventSink, IdGenerator]]:
    """A tracer over the configured tools, its events written when the block ends."""
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(db)
        with uow:
            events = _event_sink(uow, master_key, clock)
            ids = UlidIdGenerator()
            tools = build_tool_registry(
                config,
                events=events,
                ids=ids,
                clock=clock,
                transport=transport,
                credentials=bound_credentials(config, master_key),
            )
            tracer = FlowTracer(
                tools=tools,
                catalog=_build_catalog(config),
                pii=pii if pii is not None else build_pii_analyzer(config),
                events=events,
                ids=ids,
                clock=clock,
            )
            yield tracer, tools, events, ids
            uow.commit()
    finally:
        if own:
            db.dispose()


@dataclass(frozen=True, slots=True)
class StageCorrelation:
    """ADR-0021 §3: how one stage's lookup did on objects that completed the flow."""

    stage: str
    found: int
    missing: int
    unknown: int
    reasons: tuple[str, ...] = ()

    @property
    def gap(self) -> bool:
        """It never found a completed object: the stage doesn't log the id (or can't be read)."""
        return self.found == 0


def verify_flow(  # noqa: PLR0913 — the question plus the seams tests replace
    config: AppConfig,
    flow: Flow,
    samples: Sequence[str],
    *,
    engine: Engine | None = None,
    master_key: bytes | None = None,
    transport: httpx.BaseTransport | None = None,
    clock: Clock | None = None,
) -> list[StageCorrelation]:
    """``flow verify``: each stage's lookup against objects a person says completed the flow.

    A stage that finds none of them is a correlation gap: at question time it
    can only read "unknown". Drafts are verified too, so the reviewer sees
    the gaps before marking the flow reviewed. Every lookup is audited.
    """
    if not samples:
        raise ConfigError("flow verify needs at least one sample id (--sample)")  # noqa: TRY003
    bad = [s for s in samples if not flow.accepts(s)]
    if bad:
        raise ConfigError(f"sample ids the flow doesn't accept: {', '.join(bad)}")  # noqa: TRY003
    clock = clock or SystemClock()
    seen: dict[str, list[StageObservation]] = {s.id: [] for s in flow.stages}
    with _tracing(config, engine, master_key, transport, clock, None) as (tracer, _, _, ids):
        for sample in samples:
            span = ids.new(IdPrefix.EVENT)
            context = TraceContext.new(config.tenant.id, f"verify_{span}", f"span_{span}")
            run = tracer.trace(flow, sample, context, actor="verify", allow_draft=True)
            for o in run.observations:
                seen[o.stage].append(o)
    return [
        StageCorrelation(
            stage=sid,
            found=sum(1 for o in obs if o.state == "found"),
            missing=sum(1 for o in obs if o.state == "missing"),
            unknown=sum(1 for o in obs if o.state == "unknown"),
            reasons=tuple(sorted({o.reason for o in obs if o.reason})),
        )
        for sid, obs in seen.items()
    ]


def onboard_flows(
    config: AppConfig, workspace: Path, *, provider: LLMProvider | None = None
) -> tuple[list[Candidate], list[Path]]:
    """``onboard flows``: candidate flows from the history, and a draft of each, for review.

    Writes ``flows.proposed/candidates.yaml`` and one unreviewed ``<object>.yaml``
    per candidate whose tickets name at least two services of the graph.
    """

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    run = run_onboarding_pipeline(
        workspace, load_plugins(), OnboardingPipeline(config, provider=provider, decorate=cache)
    )
    graph = run.graph.graph
    tickets = [
        Ticket(i.number, f"{i.title}\n{i.description}", i.ci) for i in _past_incidents(workspace)
    ]
    found = candidates(tickets, graph)
    out = workspace / "flows.proposed"
    out.mkdir(exist_ok=True)
    header = "# ADR-0021: candidate flows from the ticket history, most asked about first.\n"
    (out / "candidates.yaml").write_text(
        header + dump_yaml({"candidates": [c.as_dict() for c in found]}), encoding="utf-8"
    )
    written: list[Path] = []
    for candidate in found:
        if len(candidate.services) < 2:  # noqa: PLR2004 — a flow needs two stages
            continue
        flow_id = re.sub(r"[^a-z0-9-]+", "-", candidate.noun.lower()).strip("-") or "object"
        draft = draft_flow(candidate, graph, flow_id=flow_id)
        Flow.model_validate(draft)  # what's written loads, once its TODOs are done
        path = out / f"{flow_id}.yaml"
        path.write_text(
            "# ADR-0021: an unreviewed draft. Name the stages, set within_min, check each\n"
            "# lookup (`supportagent flow verify`), then set reviewed: true and copy it\n"
            "# into tracing.flows_dir.\n" + dump_yaml(draft),
            encoding="utf-8",
        )
        written.append(path)
    return found, written


def build_pii_analyzer(config: AppConfig) -> PresidioAnalyzer | None:
    """ADR-0021 §4a: the configured PII analyser, loaded on first use; ``None`` when off."""
    pii = config.pii
    if pii.engine == "off":
        return None
    return PresidioAnalyzer(
        entities=pii.entities,
        min_score=pii.min_score,
        language=pii.language,
        model=pii.model,
        recognizers=[PiiRecognizerSpec(r.entity, r.regex, r.score) for r in pii.recognizers],
    )


def onboarding_facts(config: AppConfig, engine: Engine, clock: Clock) -> OnboardingFacts:
    """What ``doctor``'s onboarding check reads (ADR-0017, WP-A6)."""
    root = plugins_root()
    plugins = load_plugins(root)
    catalogs: list[tuple[str, int | None, str]] = []
    # doctor reads, never fetches: a git catalogue not fetched yet says so
    for label, directory, error in catalog_dirs(config, fetch=False):
        if directory is None:
            catalogs.append((label, None, error))
            continue
        try:
            catalogs.append((label, len(load_registry(directory)), ""))
        except (ConfigError, OSError) as exc:
            catalogs.append((label, None, str(exc)))
    declared = {
        name: p.manifest.id
        for p in plugins
        for name, spec in p.manifest.provides.capabilities.items()
        if spec.result_schema is not None and name not in CAPABILITIES
    }
    packs = _pack_set(config)
    # Only packs a catalogue service selects: a pack no service selects can't fail a case.
    try:
        services = _build_catalog(config).services
    except ConfigError:
        services = []  # the config check reports it
    used = {t for entry in services for pack in packs.active(entry.stack) for t in pack.tools}
    unserved = sorted(
        (c, pid) for c, pid in declared.items() if c in used and c not in config.tools.bindings
    )
    try:
        report = ReadinessStore(
            uow=SqlUnitOfWork(engine), clock=clock, tenant_id=config.tenant.id
        ).load()
    except sa.exc.SQLAlchemyError:
        report = None
    at = str(report["at"]) if report and report.get("at") else None
    pinned = report.get("plugins_index") if report else None
    table, why = _calibration_in_use(config, report)
    return OnboardingFacts(
        plugins=[(p.manifest.id, p.manifest.version) for p in plugins],
        disabled=disabled_plugins(root),
        catalogs=catalogs,
        unserved=unserved,
        readiness_at=at if report else None,
        readiness_present=report is not None,
        plugins_changed=None if pinned is None else pinned != index_sha256(root),
        routing=config.onboarding.routing,
        calibration=("fitted" if table is not None else "artifact", why),
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

    def shadow(self) -> ShadowReplay:
        """WP-O6c: shadow replay with this lab's model, so the replays share its recordings."""
        if self.caching is not None:
            self.caching.sample = 0
        return ShadowReplay(self._config, llm=self._llm, packs=self._packs)

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
    credentials: Mapping[str, Mapping[str, str]] | None = None,
) -> list[Tool]:
    """WP-M04, WP-O6a: the packs' and plugins' tools, each registered when configured.

    ADR-0019: ``credentials`` are the vault's bound ones, by backend
    (``first_party:oracle_ro``) and target. A target ``tools.oracle`` configures
    keeps its configured DSN: the reviewed config wins.
    """
    tools: list[Tool] = []
    bound = credentials or {}
    if cfg.postgres:  # WP-M04
        reader = PsycopgReader(
            {name: db.dsn.get_secret_value() for name, db in cfg.postgres.databases.items()},
            statement_timeout_ms=cfg.postgres.statement_timeout_ms,
        )
        tools.extend(tool_cls(reader=reader) for tool_cls in POSTGRES_TOOLS)
    if cfg.records:  # ADR-0021 §4
        records = PsycopgRecordReader(
            {name: db.dsn.get_secret_value() for name, db in cfg.records.databases.items()},
            statement_timeout_ms=cfg.records.statement_timeout_ms,
        )
        tools.append(RecordsQueryTool(reader=records))
    oracle_dsns = dict(bound.get("first_party:oracle_ro", {}))
    if cfg.oracle:
        oracle_dsns |= {
            name: db.dsn.get_secret_value() for name, db in cfg.oracle.databases.items()
        }
    if oracle_dsns:  # WP-O6a: the oracle plugin's first-party backend
        ora = OracledbReader(
            oracle_dsns, call_timeout_ms=cfg.oracle.call_timeout_ms if cfg.oracle else 5000
        )
        tools.extend(tool_cls(reader=ora) for tool_cls in ORACLE_TOOLS)
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


def build_tool_registry(  # noqa: PLR0913 — the config, its event seams and what it reaches
    config: AppConfig,
    *,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
    transport: httpx.BaseTransport | None = None,
    credentials: Mapping[str, Mapping[str, str]] | None = None,
) -> ToolRegistry:
    """WP-107: the configured read tools behind the registry (class/tier checked at load).

    ADR-0019: ``credentials`` (:func:`bound_credentials`) add the targets whose
    credentials were bound after their probes proved them read-only.

    ``gha_500`` (``SUPPORTAGENT_FAULT_INJECT``) makes the GitHub Actions tool
    fail as if its API were down (WP-106).
    """
    cfg = config.tools
    tools: list[Tool] = []
    if cfg.logs_root:  # the catalog only bounds file logs: onboarding may have none yet
        allow = frozenset(p for s in _build_catalog(config).services for p in s.log_paths)
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
    tools.extend(
        _pack_tools(cfg, client=client, clock=clock, transport=transport, credentials=credentials)
    )
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
        known = capability_catalog().get(capability)
        if known is None:
            raise ConfigError(  # noqa: TRY003
                f"tools.bindings.{capability}: not a known capability "
                f"({', '.join(sorted(capability_catalog()))})"
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


#: ADR-0021 §6: the trace lab, one directory per flow fixture, and its recordings.
TRACE_DIR = Path("evals/trace")
_APP = re.compile(r'^\{app="([a-z0-9-]+)"\}')


class _ScenarioLogs:
    """``logs.search`` answering from one trace scenario's lines; a down app fails."""

    name = "logs.search"
    trust_class = TrustClass.READ
    tier = Tier.T0
    result_schema = LOKI_SCHEMA
    literal_args = frozenset({"query", "window_min", "limit"})

    def __init__(self, scenario: TraceScenario) -> None:
        self._scenario = scenario

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        query, window, _ = render_logql(args)
        match = _APP.match(query)
        app = match.group(1) if match else ""
        if app in self._scenario.down:
            raise ToolError(f"logs.search: the log store for {app} returned 503")  # noqa: TRY003
        rows = [
            (
                int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()) * 10**9,
                line,
                {"app": app},
            )
            for ts, line in self._scenario.logs.get(app, [])
            if self._scenario.object_id in line
        ]
        return streams_result(query, window, rows)


class _SeededPii:
    """The lab's PII analyser: the values a scenario seeded, wherever they appear."""

    def __init__(self, scenario: TraceScenario) -> None:
        self._seeded = [(p.value, p.entity) for p in scenario.pii]

    def analyze(self, text: str) -> list[PiiSpan]:
        spans: list[PiiSpan] = []
        for value, entity in self._seeded:
            start = text.find(value)
            while start != -1:
                spans.append(PiiSpan(start, start + len(value), entity, 1.0))
                start = text.find(value, start + 1)
        return spans


class _CaptureRequests:
    """Keeps the text of every request the provider is sent (the PII-leak gate reads it)."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.texts: list[str] = []

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.texts.append(request.system + "\n" + request.user)
        return self._inner.complete_json(request)


class TraceLabPipeline:
    """ADR-0021 §6: one flow's scenarios through the real tracer and explainer.

    The logs are the scenario's; the model is the configured one (``live``), a
    replay of recorded answers, or a scripted provider in tests. Every request
    passes the redactor and is captured for the leak gate.
    """

    def __init__(
        self,
        config: AppConfig,
        flow: Flow,
        *,
        provider: LLMProvider | None = None,
        decorate: Callable[[LLMProvider], LLMProvider] | None = None,
    ) -> None:
        cfg = _lab_config(config)
        slots = dict(cfg.llm.slots)
        for slot in (SlotName.EXPLAIN_TRACE.value, SlotName.CHECK_TRACE.value):
            slots.setdefault(slot, slots[SlotName.CLASSIFY.value])
        self._config = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"slots": slots})})
        self.flow = flow
        self.model_id = slots[SlotName.EXPLAIN_TRACE.value].model_id
        self._clock = ReplayClock()
        self._ids = UlidIdGenerator()
        holder: list[_CaptureRequests] = []

        def capture(inner: LLMProvider) -> LLMProvider:
            wrapped = _CaptureRequests(decorate(inner) if decorate else inner)
            holder.append(wrapped)
            return wrapped

        self._llm = _build_llm(
            self._config,
            _NullEvents(),
            self._ids,
            self._clock,
            provider,
            decorate=capture,
            resilient=False,
        )
        self._capture = holder[0]
        names = sorted({s.component.split(":", 1)[-1] for s in flow.stages})
        self._catalog = Catalog(version="lab", services=[ServiceEntry(name=n) for n in names])

    def run(self, scenario: TraceScenario) -> TraceOutcome:
        self._clock.set(datetime.fromisoformat(scenario.now.replace("Z", "+00:00")))
        events = _NullEvents()
        tools = ToolRegistry(
            [_ScenarioLogs(scenario)], events=events, ids=self._ids, clock=self._clock, redact=str
        )
        tracer = FlowTracer(
            tools=tools,
            catalog=self._catalog,
            pii=_SeededPii(scenario),
            events=events,
            ids=self._ids,
            clock=self._clock,
        )
        trace = TraceContext(self._config.tenant.id, f"trace-{scenario.id}", "trace-lab")
        run = tracer.trace(self.flow, scenario.object_id, trace, actor="lab")
        self._capture.texts.clear()
        answer = TraceExplainer(llm=self._llm, catalog=self._catalog).explain(
            self.flow, run, trace, pseudonymise=run.pseudonymise
        )
        v = run.verdict
        return TraceOutcome(
            kind=v.kind,
            stage=v.stage,
            late=v.late,
            reason=v.reason,
            requests=tuple(self._capture.texts),
            business_kept=len(answer.business),
            dropped=answer.dropped,
        )


def load_trace_suites(config: AppConfig) -> tuple[list[tuple[Flow, TraceSuite]], TraceGates]:
    """ADR-0021 §6: every flow fixture under ``evals/trace`` with its scenarios, and the gates."""
    suites: list[tuple[Flow, TraceSuite]] = []
    try:
        for flow_file in sorted(TRACE_DIR.glob("*/flow.yaml")):
            flow = load_flow_file(flow_file)
            suite = TraceSuite.model_validate(
                load_yaml_document(flow_file.parent / "scenarios.yaml")
            )
            if suite.flow != flow.flow:
                raise ConfigError(  # noqa: TRY003
                    f"{flow_file.parent}: scenarios are for {suite.flow}, not {flow.flow}"
                )
            suites.append((flow, suite))
        gates = TraceGates.model_validate(
            load_yaml_document(Path(config.paths.gates)).get("trace") or {}
        )
    except ValidationError as exc:
        raise ConfigError(f"trace lab: {exc}") from exc  # noqa: TRY003
    return suites, gates


def build_trace_pipeline(
    config: AppConfig, flow: Flow, *, live: bool
) -> tuple[TraceLabPipeline, CachingLLMProvider]:
    """``live`` calls the configured model and records; otherwise replay only."""
    holder: list[CachingLLMProvider] = []

    def cache(inner: LLMProvider) -> LLMProvider:
        caching = CachingLLMProvider(inner, directory=TRACE_DIR / "recordings", snapshot_id="trace")
        holder.append(caching)
        return caching

    pipeline = TraceLabPipeline(
        config, flow, provider=None if live else _UnrecordedProvider(), decorate=cache
    )
    return pipeline, holder[0]


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
    calibration_in_use: Callable[[], tuple[str, str]] | None = None,
    flows: Callable[[], Mapping[str, Flow]] | None = None,
    trace: Callable[[str, str, str], tuple[TraceRun, TraceAnswer | None]] | None = None,
    validate_manifest: Callable[[str], str] | None = None,
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

    def _audited() -> tuple[SqlUnitOfWork, OnboardingAudit]:
        """One unit of work for a review and its audit event: both commit or neither."""
        uow = SqlUnitOfWork(engine)
        audit = OnboardingAudit(
            events=_event_sink(uow, master_key, clock), ids=ids, clock=clock, tenant_id=tenant_id
        )
        return uow, audit

    def graph_review() -> GraphReview:
        uow, audit = _audited()
        return GraphReview(uow=uow, clock=clock, tenant_id=tenant_id, audit=audit)

    def request_review() -> RequestReview:
        uow, audit = _audited()
        return RequestReview(uow=uow, clock=clock, tenant_id=tenant_id, audit=audit)

    def onboarding_history() -> list[dict[str, object]]:
        uow = SqlUnitOfWork(engine)
        with uow:
            return audit_history(_event_sink(uow, master_key, clock), tenant_id)

    def readiness() -> ReadinessStore:
        return ReadinessStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant_id)

    def onboarding_status() -> OnboardingStatusStore:
        return OnboardingStatusStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant_id)

    def onboarding_control() -> OnboardingControl:
        store = OnboardingControlStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant_id)
        return OnboardingControl(store, clock, validate=validate_manifest)

    def component_summaries() -> ComponentSummaryStore:
        return ComponentSummaryStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant_id)

    return ApiContext(
        setup=setup,
        approvals=approvals,
        queries=queries,
        stats=stats,
        replay=replay,
        lifecycle=lifecycle,
        comms_plans=comms_plans,
        webhooks=webhooks,
        graph_review=graph_review,
        request_review=request_review,
        readiness=readiness,
        onboarding_status=onboarding_status,
        onboarding_history=onboarding_history,
        onboarding_control=onboarding_control,
        component_summaries=component_summaries,
        calibration_in_use=calibration_in_use,
        flows=flows,
        trace=trace,
        schema=SqlSchemaProbe(engine, head=_head_revision()),
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
    engine = make_engine(config.database.url.get_secret_value())
    return api_context_for(
        engine,
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
        calibration_in_use=lambda: calibration_source(config, engine, clock),
        flows=lambda: {k: f for k, f in load_flows(config).items() if f.reviewed},
        validate_manifest=validate_manifest_text,
        trace=lambda flow, object_id, actor: trace_object(
            config, flow, object_id, actor=actor, engine=engine, master_key=key, clock=clock
        ),
    )


def calibration_source(config: AppConfig, engine: Engine, clock: Clock) -> tuple[str, str]:
    """``("fitted" | "artifact", why)``: what triage scores with now (ADR-0017, WP-A6)."""
    store = ReadinessStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=config.tenant.id)
    table, why = _calibration_in_use(config, store.load())
    return ("fitted" if table is not None else "artifact", why)


def _promotion_gate(config: AppConfig) -> PromotionGate:
    """``paths.gates`` when present; otherwise the CAP §4.7 provisional defaults."""
    path = Path(config.paths.gates)
    return load_promotion_gate(path) if path.is_file() else PromotionGate()


def _webhook_sources(config: AppConfig) -> dict[str, WebhookSource]:
    return {
        w.source: WebhookSource(
            secret=w.secret.get_secret_value().encode(),
            mapping=w.mapping,
            kind=w.kind,
            service_labels=tuple(w.service_labels),
        )
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

ONBOARDING_DIR = _shipped("evals", "onboarding")
#: The built-in classification labels (``incident_catalogue``'s default).
TAXONOMY_FILE = _shipped("taxonomy", "classification.yaml")


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

# A source checkout keeps plugins/ at the repo root; the image carries it in its
# working directory, where a deployment may mount its own.
PLUGINS_DIR = _shipped("plugins")


def plugins_root() -> Path:
    """Where installed plugins live (ADR-0017): ``SUPPORTAGENT_PLUGINS_DIR`` if set (a
    writable directory an instance installs into), else the shipped ``plugins/``."""
    return plugins_dir_override() or PLUGINS_DIR


def embedded_pack(plugin: LoadedPlugin) -> Path | None:
    """A plugin's own pack (``<plugin>/pack``), when it carries one (ADR-0017)."""
    directory = plugin.directory / "pack"
    return directory if (directory / "pack.yaml").is_file() else None


def _index_stamp(root: Path) -> tuple[int, int]:
    """The index's identity: a re-pin (an install) changes it, so caches follow it."""
    stat = (root / "index.yaml").stat()
    return (stat.st_mtime_ns, stat.st_size)


@functools.cache
def _embedded_packs(root: Path, stamp: tuple[int, int]) -> tuple[tuple[str, Path], ...]:
    """Packs that live inside their plugins (ADR-0017), per index version."""
    return tuple(
        (str(p.manifest.provides.pack), directory)
        for p in load_plugins(root)
        if p.manifest.provides.pack and (directory := embedded_pack(p)) is not None
    )


def load_plugins(root: Path | None = None, *, packs_dir: Path | None = None) -> list[LoadedPlugin]:
    """The pinned plugin registry; each plugin's pack, if any, must exist (in the
    plugin itself, or under ``packs/``)."""
    plugins = load_registry(root if root is not None else plugins_root())
    packs = packs_dir if packs_dir is not None else _shipped("packs")
    for plugin in plugins:
        pack = plugin.manifest.provides.pack
        if pack is None or embedded_pack(plugin) is not None:
            continue
        if not (packs / pack / "pack.yaml").is_file():
            raise ConfigError(  # noqa: TRY003
                f"plugin {plugin.manifest.id}: pack {pack!r} is neither in it nor under {packs}"
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
                url=str(meta["url"]) if meta.get("url") else None,
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
    installed = [
        ci for ci in cmdb.get("cis", []) if str(ci.get("install_status", "")).lower() != "retired"
    ]
    return SourceSnapshot(
        files=files,
        docs=docs,
        incidents=incidents,
        cmdb_classes=classes,
        git_hosts=hosts,
        repos=tuple(str(r["repo"]) for r in repos if r.get("repo")),
        cis=tuple((str(ci["name"]), str(ci["sys_class_name"])) for ci in installed),
    )


def detect_plugins(path: Path, plugins: Sequence[LoadedPlugin] | None = None) -> list[Detection]:
    """Which plugins the system at ``path`` needs, with the evidence for each."""
    registry = plugins if plugins is not None else load_plugins()
    return detect([p.manifest for p in registry], snapshot_from_sources(path))


def reference_system_dirs(root: Path = ONBOARDING_DIR) -> list[Path]:
    return sorted(d for d in root.iterdir() if (d / "truth.yaml").is_file())


_ONBOARDING_SLOTS = (SlotName.EXTRACT_GRAPH, SlotName.MAP_INCIDENTS, SlotName.SUMMARIZE_COMPONENT)


def _onboarding_config(config: AppConfig) -> AppConfig:
    """The config onboarding's LLM calls run with (ADR-0015 decision 3).

    With ``llm.onboarding`` set, the frontier model serves the onboarding
    slots (``extract_graph``, ``map_incidents``, ``summarize_component``); otherwise each
    falls back to
    the triage model (``classify``).
    """
    llm = config.llm
    slots = dict(llm.slots)
    frontier = llm.onboarding
    if frontier is None:
        for slot in _ONBOARDING_SLOTS:
            slots.setdefault(slot.value, slots[SlotName.CLASSIFY.value])
        return config.model_copy(update={"llm": llm.model_copy(update={"slots": slots})})
    for slot in _ONBOARDING_SLOTS:
        slots[slot.value] = LLMSlot(
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


class OnboardingPipeline:
    """Onboarding's model calls over redacted sources (INV-06).

    WP-O3: ``extract_graph`` over one documentation page. WP-O4:
    ``map_incidents`` over a batch of past incidents.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        provider: LLMProvider | None = None,
        decorate: Callable[[LLMProvider], LLMProvider] | None = None,
    ) -> None:
        self._config = _onboarding_config(config)
        self.model_id = self._config.llm.slots[SlotName.EXTRACT_GRAPH.value].model_id
        #: WP-222: a summary written by another model or prompt is written again.
        self.summary_version = ":".join(
            [
                self._config.llm.slots[SlotName.SUMMARIZE_COMPONENT.value].model_id,
                _prompts(self._config).fingerprint(SlotName.SUMMARIZE_COMPONENT),
            ]
        )
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

    def summarize(self, component: str, facts: str, code: str, known: str) -> SummaryAnswer | None:
        trace = TraceContext(
            self._config.tenant.id, f"summary-{abs(hash(component)) % 10**8}", "summary"
        )
        variables = {
            "component": component,
            "facts": facts,
            "code": code,
            "known_entities": known,
        }
        return self._llm.structured(SlotName.SUMMARIZE_COMPONENT, variables, SummaryAnswer, trace)

    def read_incidents(self, labels: str, causes: str, incidents: str) -> IncidentsAnswer | None:
        trace = TraceContext(
            self._config.tenant.id, f"incidents-{abs(hash(incidents)) % 10**8}", "incidents"
        )
        variables = {"labels": labels, "causes": causes, "incidents": incidents}
        return self._llm.structured(SlotName.MAP_INCIDENTS, variables, IncidentsAnswer, trace)


def build_onboarding_pipeline(config: AppConfig, *, live: bool) -> OnboardingPipeline:
    """``live`` calls the model and records; otherwise replay ``evals/onboarding/recordings``."""
    holder: list[CachingLLMProvider] = []

    def cache(inner: LLMProvider) -> LLMProvider:
        caching = CachingLLMProvider(inner, directory=ONBOARDING_RECORDINGS, snapshot_id="graph")
        holder.append(caching)
        return caching

    pipeline = OnboardingPipeline(
        config, provider=None if live else _UnrecordedProvider(), decorate=cache
    )
    pipeline.caching = holder[0] if holder else None
    return pipeline


#: WP-O6b: each reference system's per-class readiness the lab must keep.
READINESS_BASELINE = "readiness_baseline.json"


def build_readiness_lab(config: AppConfig, *, live: bool) -> LabPipeline:
    """The readiness replays: ``live`` calls the triage model and records into
    ``evals/onboarding/recordings``; otherwise replay only."""
    return build_lab_pipeline(
        config, recordings=ONBOARDING_RECORDINGS, live=live, pack_id="readiness"
    )


def read_readiness_baseline(root: Path) -> dict[str, dict[str, str]]:
    path = root / READINESS_BASELINE
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    systems = raw.get("systems", {}) if isinstance(raw, dict) else {}
    return {str(k): {str(c): str(s) for c, s in v.items()} for k, v in systems.items()}


def write_readiness_baseline(root: Path, reports: Sequence[ReadinessReport]) -> Path:
    path = root / READINESS_BASELINE
    body = {
        "v": 1,
        "model_id": reports[0].model_id if reports else None,
        "systems": {r.system: r.statuses() for r in reports},
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def discovery_rules(plugins: Sequence[LoadedPlugin]) -> list[tuple[str, str, Discovery]]:
    """Each plugin's live mapping (ADR-0016): ``(plugin id, capability, discovery)``."""
    return [
        (p.manifest.id, capability, spec)
        for p in plugins
        for capability, spec in sorted(p.manifest.provides.discovery.items())
    ]


def build_system_graph(path: Path, pipeline: OnboardingPipeline | None) -> GraphBuild:
    """The system graph of a workspace (or a reference system): structured, then prose."""
    snapshot = snapshot_from_sources(path)
    sources = path / "sources"
    cmdb = load_yaml_document(sources / "servicenow" / "cmdb.yaml")
    live_path = sources / "live" / "discovery.yaml"
    live = load_yaml_document(live_path) if live_path.is_file() else {}
    plugins = load_plugins()
    declared = [(p.manifest.id, e) for p in plugins for e in p.manifest.provides.extractors]
    facts = structured_facts(snapshot.files, cmdb, live, declared, discovery_rules(plugins))
    return build_graph(facts, snapshot.docs, pipeline.extract if pipeline is not None else None)


def incident_catalogue(
    detections: Sequence[Detection],
    plugins: Sequence[LoadedPlugin],
    taxonomy: Path = TAXONOMY_FILE,
) -> Catalogue:
    """The built-in labels and causes, plus those of the plugins the system needs."""
    needed = set(detected_ids(detections))
    chosen = [p.manifest for p in plugins if p.manifest.id in needed]
    return catalogue(
        [
            *((label.id, label.description) for label in load_classification(taxonomy).labels),
            *((label.id, label.description) for m in chosen for label in m.provides.labels),
        ],
        [(c.id, c.description) for m in chosen for c in m.provides.causes],
    )


def _past_incidents(path: Path) -> list[PastIncident]:
    servicenow = path / "sources" / "servicenow"
    problems_path = servicenow / "problems.yaml"
    return past_incidents(
        load_yaml_records(servicenow / "incidents.yaml"),
        load_yaml_records(problems_path) if problems_path.is_file() else [],
    )


def build_gap_plan(
    path: Path,
    detections: Sequence[Detection],
    plugins: Sequence[LoadedPlugin],
    graph: SystemGraph,
    incidents: IncidentMap,
) -> GapPlan:
    """WP-O5: the requests the detected plugins' contracts make, ranked by what they unlock."""
    needed = set(detected_ids(detections))
    chosen = [p.manifest for p in plugins if p.manifest.id in needed]
    cis = {i.number: i.ci for i in _past_incidents(path)}
    return gap_plan(chosen, graph, snapshot_from_sources(path), incidents, cis)


def build_incident_map(
    path: Path,
    detections: Sequence[Detection],
    plugins: Sequence[LoadedPlugin],
    pipeline: OnboardingPipeline | None,
) -> IncidentMap:
    """The incident map of a workspace (or a reference system), WP-O4."""
    history = _past_incidents(path)
    cat = incident_catalogue(detections, plugins)
    return map_incidents(history, cat, pipeline.read_incidents if pipeline is not None else None)


@dataclass(frozen=True, slots=True)
class OnboardingRun:
    result: OnboardingResult
    detections: list[Detection]
    graph: GraphBuild
    incidents: IncidentMap
    plan: GapPlan


def run_onboarding_pipeline(
    path: Path,
    plugins: Sequence[LoadedPlugin] | None = None,
    pipeline: OnboardingPipeline | None = None,
) -> OnboardingRun:
    """What the onboarding stages built so far produce for the system at ``path``.

    WP-O1: plugin detection. WP-O3: the system graph. WP-O4: the incident map.
    WP-O5: the gap plan's requests.
    Without a ``pipeline`` the model-backed parts (prose, readings) are left out.
    """
    registry = plugins if plugins is not None else load_plugins()
    detections = detect_plugins(path, registry)
    built = build_system_graph(path, pipeline)
    incidents = build_incident_map(path, detections, registry, pipeline)
    gaps = build_gap_plan(path, detections, registry, built.graph, incidents)
    result = OnboardingResult(
        plugins=detected_ids(detections),
        nodes=[ResultNode(id=n.id, kind=n.kind) for n in built.graph.nodes.values()],
        edges=[ResultEdge(src=a, kind=b, dst=c) for a, b, c in built.graph.edges],
        incident_classes=[
            ResultClass(
                id=c.id, label=c.label or "unknown", cause=c.cause, incidents=list(c.incidents)
            )
            for c in incidents.classes
        ],
        requests=[
            ResultRequest(plugin=r.plugin, access=r.access, target=r.target, scope=r.scope)
            for r in gaps.requests
        ],
    )
    return OnboardingRun(result, detections, built, incidents, gaps)


# --- WP-O6b: readiness from the system's own history ---------------------------------------


def load_evidence(path: Path) -> dict[str, dict[str, WorldEntry]]:
    """``<path>/sources/evidence/*.yaml``: what the read-only tools returned per past incident.

    Each world is validated like a lab scenario's: known tools, required fields.
    """
    directory = path / "sources" / "evidence"
    out: dict[str, dict[str, WorldEntry]] = {}
    for file in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        doc = load_yaml_document(file)
        world: dict[str, WorldEntry] = {}
        for tool, raw in (doc.get("world") or {}).items():
            if tool not in KNOWN_TOOL_SCHEMAS:
                raise ConfigError(f"{file}: evidence names unknown tool {tool!r}")  # noqa: TRY003
            try:
                entry = WorldEntry.model_validate(raw)
            except ValidationError as exc:
                raise ConfigError(f"{file}: {tool}: {exc}") from exc  # noqa: TRY003
            if entry.result is not None:
                check_shape(file, tool, entry.result, KNOWN_TOOL_SCHEMAS[tool])
            world[str(tool)] = entry
        out[str(doc.get("incident") or file.stem)] = world
    return out


def load_unreplayed(path: Path) -> dict[str, str]:
    """ADR-0018: per captured incident left without a world, why (``live_only``, ...)."""
    directory = path / "sources" / "evidence"
    out: dict[str, str] = {}
    for file in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        doc = load_yaml_document(file)
        if why := doc.get("unreplayed"):
            out[str(doc.get("incident") or file.stem)] = str(why)
    return out


@dataclass(frozen=True, slots=True)
class EvidenceCapture:
    """What ``onboard evidence`` did (ADR-0018)."""

    #: Incidents written with a world to replay.
    captured: int
    #: Incidents written without one, by why (``live_only``, ``aged_out``, ...).
    unreplayed: Mapping[str, int]
    #: Incidents whose evidence was already there (a fixture, an earlier capture).
    kept: int

    def line(self) -> str:
        why = ", ".join(f"{n} {w.replace('_', ' ')}" for w, n in sorted(self.unreplayed.items()))
        return (
            f"evidence for {self.captured} incident(s)"
            + (f"; {sum(self.unreplayed.values())} not replayable ({why})" if why else "")
            + (f"; {self.kept} kept" if self.kept else "")
        )


_EVIDENCE_HEADER = (
    "# Captured by `supportagent onboard evidence` (ADR-0018): the read-only tools, as of\n"
    "# the incident. Re-capture with --recapture; never edit by hand.\n"
)


def capture_evidence(  # noqa: PLR0913 — the run, the tools and the seams
    config: AppConfig,
    workspace: Path,
    run: OnboardingRun,
    tools: ToolRegistry,
    *,
    clock: Clock,
    plugins: Sequence[LoadedPlugin] | None = None,
    recapture: bool = False,
) -> EvidenceCapture:
    """ADR-0018 (WP-E1): each past incident of a testable class, read as of its open time.

    The reads are the probes RCA would plan for it (its label's runbooks in the
    packs its service selects, and the cause's probe), through the registry, so
    each is bound, schema-checked and a ``tool_call`` event.
    """
    directory = workspace / "sources" / "evidence"
    existing = {f.stem for f in directory.glob("*.yaml")} if directory.is_dir() else set()
    why_not = readiness_testable(config, plugins if plugins is not None else load_plugins())
    knowledge = build_rca_knowledge(config, tools.names)
    packs = _pack_set(config)
    incidents = {i.number: i for i in _past_incidents(workspace)}
    trace = TraceContext(tenant_id=config.tenant.id, trace_id="evidence", span_id="capture")
    captured, kept = 0, 0
    unreplayed: dict[str, int] = {}
    for cls in run.incidents.classes:
        if cls.cause is None or why_not(cls.cause) is not None:
            continue
        hypothesis = cause_hypothesis(cls.cause)
        probe = knowledge.probes.get(hypothesis)
        for number in cls.incidents:
            incident = incidents.get(number)
            if incident is None:
                continue
            if number in existing and not recapture:
                kept += 1
                continue
            service = ServiceEntry.model_validate(service_entry(run.graph.graph, incident.ci))
            selected = packs.runbook_ids(packs.active(service.stack))
            plan = capture_plan(
                cls.label,
                hypothesis,
                [r for r, _ in knowledge.runbooks if r.id in selected],
                probe[0] if probe else None,
            )

            def read(
                item: PlannedRead, at: datetime = incident.opened, svc: ServiceEntry = service
            ) -> Mapping[str, object]:
                return tools.call_as_of(item.capability, item.args, svc, trace, at=at).data

            evidence = capture(
                number,
                incident.opened,
                plan,
                now=clock.now(),
                retention_days=config.onboarding.retention_days,
                available=lambda c: c in tools.names,
                replayable=tools.replayable,
                read=read,
            )
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{number}.yaml").write_text(
                _EVIDENCE_HEADER + dump_yaml(evidence.as_document()), encoding="utf-8"
            )
            if evidence.unreplayed is None:
                captured += 1
            else:
                unreplayed[evidence.unreplayed] = unreplayed.get(evidence.unreplayed, 0) + 1
    return EvidenceCapture(captured, unreplayed, kept)


def readiness_testable(
    config: AppConfig, plugins: Sequence[LoadedPlugin]
) -> Callable[[str], str | None]:
    """``why_not(cause)``: ``None`` when an evaluated pack (or common) can test the cause."""
    knowledge = build_rca_knowledge(config, KNOWN_TOOL_SCHEMAS)
    tested = set(knowledge.probes) | {s.hypothesis for r, _ in knowledge.runbooks for s in r.steps}
    owner = {c.id: p.manifest for p in plugins for c in p.manifest.provides.causes}

    def why_not(cause: str) -> str | None:
        plugin = owner.get(cause)
        if plugin is not None and plugin.status != "evaluated":
            return f"the {plugin.id} plugin is a skeleton: no knowledge of {cause} is evaluated"
        if cause_hypothesis(cause) not in tested:
            return f"no probe or runbook tests {cause}"
        return None

    return why_not


def build_readiness(  # noqa: PLR0913 — the run, the lab and what they read
    path: Path,
    run: OnboardingRun,
    lab: LabPipeline,
    config: AppConfig,
    *,
    plugins: Sequence[LoadedPlugin] | None = None,
    manifest: SystemManifest | None = None,
    shadow: ShadowReplay | None = None,
    live: Mapping[str, LiveRca] | None = None,
) -> ReadinessReport:
    """WP-O6b: replay each testable class's history in the lab and assess it.

    WP-O6c: ``live`` (the agent's RCA on live incidents, in shadow) stands in for
    the evidence an incident has none of. ``shadow`` then runs the history
    through the shadow triage pipeline and applies its gate.
    """
    registry = plugins if plugins is not None else load_plugins()
    system = manifest or load_system_manifest(path / "system.yaml", {}, need_secrets=False)
    why_not = readiness_testable(config, registry)
    past = _past_incidents(path)
    scenarios, missing = history_scenarios(
        run.incidents,
        past,
        run.graph.graph,
        load_evidence(path),
        system=system.system.id,
    )
    causes = {c.id: c.cause for c in run.incidents.classes}
    replayed: dict[str, list[Replayed]] = {}
    missing = dict(missing)
    by_number = {i.number: i for i in past}
    seen_live = live or {}
    # WP-O6c: an incident without evidence is proven by the live RCA shadow ran on it
    rca_live = {n for n, s in seen_live.items() if s.verdict is not None}
    for cls in run.incidents.classes:
        if cls.cause is None or why_not(cls.cause) is not None:
            continue
        for number in missing.get(cls.id, ()):
            if number in rca_live:
                scenario = history_scenario(
                    cls, by_number[number], run.graph.graph, {}, system.system.id
                )
                replayed.setdefault(cls.id, []).append(
                    _live_replayed(scenario, number, seen_live[number])
                )
        missing[cls.id] = tuple(n for n in missing.get(cls.id, ()) if n not in rca_live)
    # ... and every resolved live case is a calibration observation of its class's label
    observed = [
        (
            seen.classification,
            seen.preclass,
            seen.classification is not None
            and prefix_match(cls.label or "request/access", seen.classification),
        )
        for cls in run.incidents.classes
        for n in cls.incidents
        if (seen := seen_live.get(n)) is not None
    ]
    for item in scenarios:
        cause = causes.get(item.class_id)
        if cause is not None and why_not(cause) is not None:
            continue  # untestable: nothing to replay
        outcome = lab.run(item.scenario, 0)
        supported = outcome.supported if outcome.verdict == "SUPPORTED" else None
        replayed.setdefault(item.class_id, []).append(
            Replayed(
                item.incident,
                supported,
                score_outcome(item.scenario, outcome),
                predicted=outcome.classification,
                preclass=outcome.preclass_label,
            )
        )
    report = assess(
        run.incidents,
        replayed,
        missing,
        unreplayed=load_unreplayed(path),
        testable=why_not,
        gates=system.readiness,
        system=system.system.id,
        model_id=lab.model_id,
    )
    report.calibration = fit_calibration(
        (r for rows in replayed.values() for r in rows if r.incident not in seen_live),
        model_id=lab.model_id,
        system=system.system.id,
        observed=observed,
    )
    if shadow is None:
        return report
    cases, gaps = shadow.run(report, run.incidents, past, run.graph.graph)
    return apply_shadow(
        report,
        run.incidents,
        cases,
        gates=system.readiness,
        min_score=config.calibration.min_score,
        catalog_gaps=gaps,
    )


def _live_replayed(scenario: LabScenario, number: str, seen: LiveRca) -> Replayed:
    """WP-O6c: a live RCA (in shadow) scored as the incident's replay would be."""
    outcome = LabOutcome(
        scenario_id=scenario.id,
        classification=seen.classification,
        severity=seen.severity,
        preclass_label=seen.preclass,
        verdict=seen.verdict or "INCONCLUSIVE",
        supported=seen.supported,
        tested=seen.tested,
        cited_tools=frozenset(),
        tool_calls=0,
        tokens=0,
    )
    return Replayed(
        number,
        seen.supported,
        score_outcome(scenario, outcome),
        predicted=seen.classification,
        preclass=seen.preclass,
        live=True,
    )


#: WP-O6c: the paging integration a replayed service is given when the catalog has none,
#: so routing is measured; the missing integration is reported as a catalog gap.
_UNCONFIGURED_PAGING = "onboarding:unconfigured"


class ShadowReplay:
    """WP-O6c: a system's history through the shadow triage pipeline (ARCH §8.4).

    Each past incident, in the order it was opened and with the clock at that
    moment, goes through ``ShadowRunner`` on a throwaway database: dedup, storm
    grouping, the spend breaker, the pre-classifier, the classifier with the
    calibration this readiness run fitted (when triage uses that model), the
    severity cap, routing by this run's readiness, and the actions that would
    have been proposed. The model is the lab's, so the replays share its
    recordings.

    The catalog is the configured one where it knows the incident's CI;
    otherwise the entry the system graph implies. Either way, a service the
    configured catalog wouldn't route (unknown, or with no paging integration,
    which triage refuses) is a *catalog gap*: reported, and given a placeholder
    integration so the class's routing is still measured.
    """

    def __init__(self, config: AppConfig, *, llm: LLMClient, packs: PackSet) -> None:
        self._config = config
        self._llm = llm
        self._packs = packs

    def run(
        self,
        report: ReadinessReport,
        incident_map: IncidentMap,
        incidents: Sequence[PastIncident],
        graph: SystemGraph,
    ) -> tuple[list[ShadowCase], list[tuple[str, str]]]:
        mapped = {n for c in incident_map.classes for n in c.incidents}
        history = sorted(
            (i for i in incidents if i.number in mapped), key=lambda i: (i.opened, i.number)
        )
        catalog, gaps = self._catalog(history, graph)
        with tempfile.TemporaryDirectory(prefix="supportagent-shadow-") as tmp:
            url = f"sqlite:///{(Path(tmp) / 'shadow.db').as_posix()}"
            run_migrations(url)
            engine = make_engine(url)
            try:
                return self._replay(report, history, catalog, engine), gaps
            finally:
                engine.dispose()

    def _replay(
        self,
        report: ReadinessReport,
        history: Sequence[PastIncident],
        catalog: Catalog,
        engine: Engine,
    ) -> list[ShadowCase]:
        cfg = self._config
        uow = SqlUnitOfWork(engine)
        clock = ReplayClock()
        ids = UlidIdGenerator()
        events = _event_sink(uow, secrets.token_bytes(32), clock)
        document = report.as_dict()
        fitted, _ = _calibration_in_use(cfg, document)
        classes = document["classes"]
        runner = ShadowRunner(
            uow=uow,
            clock=clock,
            ids=ids,
            events=events,
            case_repo=CaseRepo(uow),
            classifier=Classifier(
                rule_set=_build_ruleset(cfg),
                llm=self._llm,
                calibration=fitted or _classifier_calibration(cfg),
                min_score=cfg.calibration.min_score,
                routing=_build_routing(cfg),
                taxonomy_labels=labels_text(self._packs.taxonomy().labels),
                severity=_severity_strategy(cfg),
                packs=PackScope(
                    packs=self._packs,
                    stacks={s.name: frozenset(s.stack) for s in catalog.services},
                ),
                readiness=None
                if cfg.onboarding.routing == "report"
                else ReadinessPolicy.of(classes if isinstance(classes, list) else []),
            ),
            catalog=catalog,
            repo=ShadowRepo(uow),
            tenant_constants=cfg.redaction.tenant_constants,
            grouping=GroupingService(uow=uow, ids=ids, catalog=catalog, tenant_id=cfg.tenant.id),
            spend=SpendBreaker(uow=uow, daily_budget_tokens=cfg.llm.daily_budget_tokens),
        )
        first_of_case: dict[str, str] = {}
        out: list[ShadowCase] = []
        for incident in history:
            clock.set(incident.opened)
            record = runner.run(self._event(incident))
            if record is None:  # pragma: no cover — a fresh database holds only shadow cases
                continue
            merged = first_of_case.setdefault(record.case_id, incident.number)
            decision = record.proposed.get("decision")
            specs = record.proposed.get("specs")
            d = decision if isinstance(decision, dict) else {}
            out.append(
                ShadowCase(
                    incident=incident.number,
                    outcome=str(d.get("outcome", "safe_default")),
                    reason=str(d.get("reason", "")),
                    classification=d.get("classification"),
                    route=d.get("route_id"),
                    preclass=d.get("preclass_label"),
                    merged_into=None if merged == incident.number else merged,
                    actions=tuple(
                        str(spec.get("kind"))
                        for spec in (specs if isinstance(specs, list) else [])
                        if isinstance(spec, dict)
                    ),
                )
            )
        return out

    def _event(self, incident: PastIncident) -> AlertEvent:
        return AlertEvent.model_validate(
            {
                "tenant_id": self._config.tenant.id,
                "source": "servicenow",
                "origin": "ticket",
                "external_id": incident.number,
                "table": "incident",
                "ticket_number": incident.number,
                "received_at": incident.opened,
                "subject": incident.title or incident.number,
                "body": incident.description,
                "resource": None,
                "alert_type": None,
                "error_signature": None,
                "severity_hint": Severity(incident.severity)
                if incident.severity in {s.value for s in Severity}
                else None,
                "cmdb_ci": incident.ci,
                "raw_ref": f"history:{incident.number}",
            }
        )

    def _catalog(
        self, history: Sequence[PastIncident], graph: SystemGraph
    ) -> tuple[Catalog, list[tuple[str, str]]]:
        try:
            configured = _build_catalog(self._config).services
        except (ConfigError, OSError):
            configured = []
        entries: dict[str, ServiceEntry] = {}
        gaps: list[tuple[str, str]] = []
        for ci in sorted({i.ci for i in history}):
            known = next((s for s in configured if ci in s.cmdb_ci), None)
            if known is not None:
                entry = known
                if not known.paging_integration:
                    gaps.append((known.name, "no paging integration, so triage refuses it"))
                    entry = known.model_copy(update={"paging_integration": _UNCONFIGURED_PAGING})
            else:
                implied = service_entry(graph, ci)
                entry = ServiceEntry.model_validate(
                    implied | {"cmdb_ci": [ci], "paging_integration": _UNCONFIGURED_PAGING}
                )
                gaps.append((entry.name, f"not in the catalog (CMDB CI {ci}): add it"))
            prior = entries.get(entry.name)
            if prior is not None:
                entry = prior.model_copy(update={"cmdb_ci": sorted({*prior.cmdb_ci, ci})})
            entries[entry.name] = entry
        if not entries:
            entries["onboarding-none"] = ServiceEntry(name="onboarding-none")
        return Catalog(version="onboarding-shadow", services=list(entries.values())), gaps


# --- WP-O8: drift and re-onboarding --------------------------------------------------------

#: Each reference system's "some time later" change set and the diff it must produce.
DRIFT_DIR = ONBOARDING_DIR / "drift"
#: The workspace artifact that holds the last accepted onboarding snapshot.
SNAPSHOT_ARTIFACT = "onboarding_snapshot.json"


def onboarding_snapshot(run: OnboardingRun, plugins: Sequence[LoadedPlugin]) -> Snapshot:
    """What one onboarding run understood, as drift compares it."""
    graph = run.graph.graph
    return Snapshot(
        plugins=frozenset(detected_ids(run.detections)),
        nodes={n.id: n.kind for n in graph.nodes.values()},
        edges=frozenset(graph.edges),
        classes={
            c.id: ClassShape(c.cause, c.label, c.share, len(c.incidents))
            for c in run.incidents.classes
        },
        known_causes=frozenset(incident_catalogue(run.detections, plugins).causes),
        requests=frozenset("/".join(r.key) for r in run.plan.requests),
    )


def load_drift_case(system: Path) -> dict[str, object] | None:
    """``evals/onboarding/drift/<system>.yaml``, if the system has one."""
    path = DRIFT_DIR / f"{system.name}.yaml"
    return load_yaml_document(path) if path.is_file() else None


def changed_system(system: Path, case: Mapping[str, object], out: Path) -> Path:
    """The reference system with its drift change set applied, written to ``out``."""
    return apply_change_set(system, dict(case), out)


def onboard_drift(
    config: AppConfig,
    workspace: Path,
    *,
    accept: bool = False,
    provider: LLMProvider | None = None,
    record: Recorder | None = None,
) -> tuple[list[Finding], bool, Path]:
    """``onboard drift``: re-run onboarding (P1 to P4) and diff it with the accepted snapshot.

    The first run has nothing to compare with, so it is accepted. ``accept``
    takes this run as the new baseline once its findings are dealt with.
    Returns the findings, whether this run was accepted, and ``drift.json``.
    """

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    plugins = load_plugins()
    pipeline = OnboardingPipeline(config, provider=provider, decorate=cache)
    now = onboarding_snapshot(run_onboarding_pipeline(workspace, plugins, pipeline), plugins)
    space = Workspace(workspace)
    previous = workspace / SNAPSHOT_ARTIFACT
    compared = previous.is_file()
    findings = (
        diff(Snapshot.of(json.loads(previous.read_text(encoding="utf-8"))), now) if compared else []
    )
    accepted = accept or not compared
    if record is not None and findings:
        record(
            "drift_found",
            "onboarding",
            {
                "findings": len(findings),
                "accepted": accepted,
                "kinds": sorted({f.kind for f in findings}),
                "first": [f"{f.kind} {f.subject}" for f in findings[:10]],
            },
        )
    if accepted:
        space.write_artifact(SNAPSHOT_ARTIFACT, now.as_dict())
    path = space.write_artifact(
        "drift.json",
        {
            "v": 1,
            "compared": compared,
            "accepted": accepted,
            "findings": [
                {"kind": f.kind, "subject": f.subject, "detail": f.detail, "action": f.action}
                for f in findings
            ],
        },
    )
    return findings, accepted, path


def pin_plugins(root: Path | None = None) -> list[str]:
    """``supportagent plugin index``: re-pin every plugin as it is now (after review)."""
    return write_index(root if root is not None else plugins_root())


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
    transports: IngestTransports = IngestTransports(),
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


@dataclass(frozen=True, slots=True)
class OnboardGraphReport:
    nodes: int
    edges: int
    questions: int
    saved: Mapping[str, int]
    pages_read: int
    pages_skipped: int
    rejected: int
    failed_pages: Sequence[str]
    #: WP-222: component summaries written, reused (unchanged) and not written.
    summaries: SummaryRun | None = None
    #: The run's ``budget.llm_tokens`` was spent before everything was read.
    budget_spent: bool = False


def onboard_incidents(
    config: AppConfig, workspace: Path, *, provider: LLMProvider | None = None
) -> tuple[IncidentMap, Path]:
    """``onboard incidents``: map a workspace's ticket history; write ``incident_map.json``.

    The readings call the onboarding model; its answers are cached in
    ``<workspace>/recordings``, so a re-run is cheap and resumes.
    """

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    pipeline = OnboardingPipeline(config, provider=provider, decorate=cache)
    plugins = load_plugins()
    mapped = build_incident_map(workspace, detect_plugins(workspace, plugins), plugins, pipeline)
    path = Workspace(workspace).write_artifact("incident_map.json", incident_map_document(mapped))
    return mapped, path


def onboard_evidence(  # noqa: PLR0913 — the config, the workspace and the seams
    config: AppConfig,
    workspace: Path,
    *,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    clock: Clock | None = None,
    master_key: bytes | None = None,
    transport: httpx.BaseTransport | None = None,
    recapture: bool = False,
) -> EvidenceCapture | None:
    """``onboard evidence`` (ADR-0018): capture each past incident's evidence from the
    configured read-only tools, as of the incident. ``None`` when no configured tool
    can read the past: the workspace's evidence (a fixture, a capture) stays as it is.

    Every read is a ``tool_call`` event in the log, like RCA's.
    """

    def onboarding_cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    clock = clock or SystemClock()
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    try:
        uow = SqlUnitOfWork(db)
        with uow:
            tools = build_tool_registry(
                config,
                events=_event_sink(uow, master_key, clock),
                ids=UlidIdGenerator(),
                clock=clock,
                transport=transport,
                credentials=bound_credentials(config, master_key),
            )
            if not any(tools.replayable(n) for n in tools.names):
                return None
            plugins = load_plugins()
            pipeline = OnboardingPipeline(config, provider=provider, decorate=onboarding_cache)
            run = run_onboarding_pipeline(workspace, plugins, pipeline)
            result = capture_evidence(
                config, workspace, run, tools, clock=clock, plugins=plugins, recapture=recapture
            )
            uow.commit()
            return result
    finally:
        if own:
            db.dispose()


def onboard_readiness(  # noqa: PLR0913 — the config, the workspace and the seams
    config: AppConfig,
    workspace: Path,
    *,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    clock: Clock | None = None,
    master_key: bytes | None = None,
) -> tuple[ReadinessReport, Path]:
    """``onboard readiness`` (WP-O6b): replay the history per class; save the report.

    The graph and incident map come from ``onboard graph`` / ``onboard incidents``
    (their answers are in ``<workspace>/recordings``). The replays use the
    triage model, the one that will support the system, and are cached there too.
    The report is saved for routing (triage and RCA read it at their next start)
    and written as ``readiness.json``.
    """

    def onboarding_cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    def lab_cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(
            inner, directory=workspace / "recordings", snapshot_id="readiness"
        )

    plugins = load_plugins()
    pipeline = OnboardingPipeline(config, provider=provider, decorate=onboarding_cache)
    run = run_onboarding_pipeline(workspace, plugins, pipeline)
    lab = LabPipeline(config, provider=provider, decorate=lab_cache)
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    clock = clock or SystemClock()
    try:
        # WP-O6c: the history through shadow too, and what shadow proved live
        live = LiveProof(uow=SqlUnitOfWork(db), tenant_id=config.tenant.id).outcomes()
        report = build_readiness(
            workspace, run, lab, config, plugins=plugins, shadow=lab.shadow(), live=live
        )
        ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=config.tenant.id).save(
            report.as_dict()
            # WP-A6: when, and against which plugins, so doctor can say it went stale
            | {"at": clock.now().isoformat(), "plugins_index": index_sha256(plugins_root())}
        )
        record = _recorder(config, db, master_key, clock)
        record(
            "readiness_assessed",
            "onboarding",
            {
                "ready": report.ready,
                "ready_share": report.ready_share,
                "model": report.model_id,
                "classes": {c.class_id: c.status for c in report.classes},
            },
        )
        if report.calibration is not None:
            in_use = (
                report.calibration.model_id == config.llm.slots[SlotName.CLASSIFY.value].model_id
            )
            record(
                "calibration_fitted",
                "onboarding",
                {
                    "labels": len(report.calibration.labels),
                    "observations": sum(
                        (r.agree.n if r.agree else 0) + (r.abstain.n if r.abstain else 0)
                        for r in report.calibration.labels.values()
                    ),
                    "model": report.calibration.model_id,
                    "in_use": in_use,
                },
            )
    finally:
        if own:
            db.dispose()
    path = Workspace(workspace).write_artifact("readiness.json", report.as_dict())
    return report, path


def onboard_requests(
    config: AppConfig,
    workspace: Path,
    *,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    clock: Clock | None = None,
) -> tuple[GapPlan, dict[str, int], Path]:
    """``onboard requests``: the gap plan for a workspace, saved for review and as JSON.

    It builds the graph and the incident map first (the model's answers come
    from ``<workspace>/recordings`` when ``onboard graph`` and ``onboard
    incidents`` already ran).
    """

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    pipeline = OnboardingPipeline(config, provider=provider, decorate=cache)
    run = run_onboarding_pipeline(workspace, load_plugins(), pipeline)
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    try:
        review = RequestReview(
            uow=SqlUnitOfWork(db), clock=clock or SystemClock(), tenant_id=config.tenant.id
        )
        saved = review.save(run.plan)
    finally:
        if own:
            db.dispose()
    document = {
        "unlocked": run.plan.unlocked,
        "requests": [
            {
                "plugin": r.plugin,
                "access": r.access,
                "target": r.target,
                "scope": r.scope,
                "why": r.why,
                "unlocks": list(r.unlocks),
                "share": r.share,
                "found_in": r.found_in,
                "verify_can": list(r.verify_can),
                "verify_cannot": list(r.verify_cannot),
            }
            for r in run.plan.requests
        ],
        "gaps": [
            {"class": g.class_id, "cause": g.cause, "share": g.share, "why": g.why}
            for g in run.plan.gaps
        ],
    }
    path = Workspace(workspace).write_artifact("requests.json", document)
    return run.plan, saved, path


def credential_vault(config: AppConfig, master_key: bytes) -> FileCredentialVault:
    """ADR-0019: the vault of bound credentials (``onboarding.credentials_vault``)."""
    return FileCredentialVault(
        Path(config.onboarding.credentials_vault), master_key=master_key, tenant_id=config.tenant.id
    )


def bound_credentials(config: AppConfig, master_key: bytes | None) -> dict[str, dict[str, str]]:
    """ADR-0019: the vault's credentials by backend, then target, for the tool registry."""
    out: dict[str, dict[str, str]] = {}
    if not Path(config.onboarding.credentials_vault).exists():
        return out  # nothing bound: no master key needed
    key = master_key if master_key is not None else load_master_key_default()
    for cred in credential_vault(config, key).all():
        out.setdefault(cred.backend, {})[cred.target] = cred.secret
    return out


def build_access_binding(  # noqa: PLR0913 — the stores, the clock and what the probes reach
    config: AppConfig,
    *,
    engine: Engine,
    master_key: bytes,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    verifiers: Mapping[str, AccessVerifier] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> AccessBinding:
    """ADR-0019 (WP-O5b): ``onboard bind``, ``unbind`` and ``verify``.

    The review and its audit events share one unit of work; the credential
    goes to the vault. ``verifiers`` replaces the built-in ones (tests, fakes).
    """
    clock = clock or SystemClock()
    uow = SqlUnitOfWork(engine)
    audit = OnboardingAudit(
        events=_event_sink(uow, master_key, clock),
        ids=ids or UlidIdGenerator(),
        clock=clock,
        tenant_id=config.tenant.id,
    )
    return AccessBinding(
        review=RequestReview(uow=uow, clock=clock, tenant_id=config.tenant.id, audit=audit),
        store=credential_vault(config, master_key),
        verifiers=verifiers if verifiers is not None else access_verifiers(transport=transport),
        plugins=[p.manifest for p in load_plugins()],
        clock=clock,
    )


def onboard_bind(  # noqa: PLR0913 — the request, its credential and who binds it
    config: AppConfig,
    rid: str,
    secret: str,
    *,
    actor: str,
    engine: Engine | None = None,
    master_key: bytes | None = None,
    clock: Clock | None = None,
) -> BindOutcome:
    """``onboard bind``: prove a request's credential read-only and bind it, or reopen it."""
    return _with_binding(
        config, engine, master_key, clock, lambda b: b.bind(rid, secret, actor=actor)
    )


def onboard_unbind(  # noqa: PLR0913 — the request and who unbinds it
    config: AppConfig,
    rid: str,
    *,
    actor: str,
    engine: Engine | None = None,
    master_key: bytes | None = None,
    clock: Clock | None = None,
) -> RequestView:
    """``onboard unbind``: take a request's credential out of the vault."""
    return _with_binding(config, engine, master_key, clock, lambda b: b.unbind(rid, actor=actor))


def onboard_verify(
    config: AppConfig,
    *,
    actor: str = "system",
    engine: Engine | None = None,
    master_key: bytes | None = None,
    clock: Clock | None = None,
) -> list[BindOutcome]:
    """``onboard verify``: every bound credential's probes again; one that can write now
    leaves the vault and its request reopens."""
    return _with_binding(config, engine, master_key, clock, lambda b: b.reverify(actor=actor))


def _with_binding[T](
    config: AppConfig,
    engine: Engine | None,
    master_key: bytes | None,
    clock: Clock | None,
    run: Callable[[AccessBinding], T],
) -> T:
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    key = master_key if master_key is not None else load_master_key_default()
    try:
        return run(build_access_binding(config, engine=db, master_key=key, clock=clock))
    finally:
        if own:
            db.dispose()


def incident_map_document(mapped: IncidentMap) -> dict[str, object]:
    return {
        "coverage": mapped.coverage,
        "classes": [
            {
                "id": c.id,
                "label": c.label,
                "cause": c.cause,
                "incidents": list(c.incidents),
                "problems": list(c.problems),
                "changes": list(c.changes),
                "weight": c.weight,
                "share": c.share,
                "mean_hours_to_resolve": c.mean_hours_to_resolve,
                "severities": dict(c.severities),
                "quotes": list(c.quotes),
                "basis": list(c.basis),
            }
            for c in mapped.classes
        ],
        "unread": mapped.unread,
        "dropped": {n: r.dropped for n, r in sorted(mapped.readings.items()) if r.dropped},
    }


def onboard_graph(  # noqa: PLR0913 — the config, the workspace and the seams
    config: AppConfig,
    workspace: Path,
    *,
    docs: bool = True,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    clock: Clock | None = None,
) -> OnboardGraphReport:
    """``onboard graph``: build a workspace's system graph and save it for review.

    The prose layer calls the onboarding model (``llm.onboarding``, else the
    triage model); its answers are cached in ``<workspace>/recordings`` so a
    re-run is cheap and resumes. ``docs=False`` builds from code, config, the
    CMDB and live discovery only.
    """
    pipeline: OnboardingPipeline | None = None
    budgets: list[BudgetedProvider] = []
    if docs:

        def cache(inner: LLMProvider) -> LLMProvider:
            budgets.append(BudgetedProvider(inner, limit=_token_budget(workspace)))
            return CachingLLMProvider(
                budgets[-1], directory=workspace / "recordings", snapshot_id="graph"
            )

        pipeline = OnboardingPipeline(config, provider=provider, decorate=cache)
    built = build_system_graph(workspace, pipeline)
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    tenant, now = config.tenant.id, clock or SystemClock()
    summaries: SummaryRun | None = None
    if pipeline is not None:
        store = ComponentSummaryStore(uow=SqlUnitOfWork(db), clock=now, tenant_id=tenant)
        built, summaries = summarize_components(
            built,
            snapshot_from_sources(workspace).files,
            pipeline.summarize,
            store.load(),
            version=pipeline.summary_version,
        )
        store.save({"v": 1, "components": summaries.summaries})
    links = {
        f"confluence:{page.page_id}": page.url
        for page in snapshot_from_sources(workspace).docs
        if page.url
    }
    try:
        review = GraphReview(uow=SqlUnitOfWork(db), clock=now, tenant_id=tenant)
        saved = review.save(built, links)
    finally:
        if own:
            db.dispose()
    return OnboardGraphReport(
        nodes=len(built.graph.nodes),
        edges=len(built.graph.edges),
        questions=len(built.graph.questions),
        saved=saved,
        pages_read=len(built.pages_read),
        pages_skipped=len(built.pages_skipped),
        rejected=len(built.rejected),
        failed_pages=built.failed,
        summaries=summaries,
        budget_spent=any(b.refused for b in budgets),
    )


def _token_budget(workspace: Path) -> int:
    """The manifest's ``budget.llm_tokens`` for one onboarding run."""
    manifest = workspace / "system.yaml"
    if not manifest.is_file():
        return ManifestBudget().llm_tokens
    return load_system_manifest(manifest, {}, need_secrets=False).budget.llm_tokens


# --- ADR-0017 (WP-A1): the onboarding autopilot ----------------------------------------------

AUTOPILOT_STATE = "onboarding.json"


def _tree_digest(root: Path) -> list[tuple[str, str]]:
    return [
        (p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def _previous_steps(workspace: Path) -> dict[str, StepRecord]:
    path = workspace / AUTOPILOT_STATE
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    steps = raw.get("steps", []) if isinstance(raw, dict) else []
    return {r.step: r for s in steps if isinstance(s, dict) and (r := StepRecord.of(s)) is not None}


def validate_manifest_text(text: str) -> str:
    """A manifest edited on ``/ui/onboarding/system`` (ADR-0023 §2): its system id, or why not.

    Credentials stay in the environment: a manifest stored by the instance
    names its variables (``token_env``), never holds a secret itself.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.yaml"
        path.write_text(text, encoding="utf-8")
        try:
            manifest = load_system_manifest(path, {}, need_secrets=False)
        except ConfigError as exc:
            raise DomainValidationError(str(exc).replace(str(path), "the manifest")) from exc
    inline = [f"git {g.repo}" for g in manifest.sources.git if g.token is not None] + [
        f"confluence {c.space}" for c in manifest.sources.confluence if c.user or c.token
    ]
    if inline:
        raise DomainValidationError(  # noqa: TRY003
            "credentials go in environment variables (token_env, user_env), not in the "
            f"manifest: {', '.join(inline)}"
        )
    return manifest.system.id


def onboarding_control_for(config: AppConfig, engine: Engine, clock: Clock) -> OnboardingControl:
    store = OnboardingControlStore(
        uow=SqlUnitOfWork(engine), clock=clock, tenant_id=config.tenant.id
    )
    return OnboardingControl(store, clock, validate=validate_manifest_text)


def onboard_system(  # noqa: PLR0913 — the inputs and the seams
    config: AppConfig,
    *,
    manifest: Path | None = None,
    run: bool = False,
    actor: str = "cli",
    engine: Engine | None = None,
    clock: Clock | None = None,
) -> dict[str, object]:
    """``onboard system`` (ADR-0023 §2): set the stored manifest and ask for a run, without a UI.

    For the installer and its answers file. The worker's ``onboard.autopilot`` runs it.
    """
    clock = clock or SystemClock()
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    try:
        control = onboarding_control_for(config, db, clock)
        if manifest is not None:
            try:
                control.save_manifest(manifest.read_text(encoding="utf-8"), actor=actor)
            except DomainValidationError as exc:
                raise ConfigError(str(exc)) from exc
        if run:
            try:
                control.request_run(actor=actor)
            except DomainValidationError as exc:
                raise ConfigError(str(exc)) from exc
        return control.state()
    finally:
        if own:
            db.dispose()


def run_autopilot(  # noqa: PLR0913 — the config and the seams
    config: AppConfig,
    *,
    engine: Engine,
    master_key: bytes | None = None,
    clock: Clock | None = None,
    llm_provider: LLMProvider | None = None,
    env: Mapping[str, str] | None = None,
) -> OnboardingStatus | None:
    """One onboarding run inside the instance (ADR-0023 §2), for ``onboard.autopilot``.

    The stored manifest is written into the instance's workspace, and every
    step runs as ``onboard run`` would; how it ended is recorded for the UI.
    """
    clock = clock or SystemClock()
    control = onboarding_control_for(config, engine, clock)
    manifest = control.manifest()
    if manifest is None:
        control.finish(ok=False, detail="no manifest")
        return None
    workspace = Path(config.onboarding.workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "system.yaml"
        path.write_text(manifest, encoding="utf-8")
        status = onboard_run(
            config,
            path,
            workspace,
            provider=llm_provider,
            engine=engine,
            clock=clock,
            env=env,
            master_key=master_key,
        )
    except (ConfigError, ExternalServiceError, OSError) as exc:
        control.finish(ok=False, detail=str(exc))
        raise
    failed = [s.step for s in status.steps if s.state == "failed"]
    detail = f"{len(status.steps)} step(s); " + (
        f"failed: {', '.join(failed)}" if failed else f"{len(status.needs)} need(s)"
    )
    control.finish(ok=not failed, detail=detail)
    return status


def onboard_run(  # noqa: PLR0913 — the inputs and the seams
    config: AppConfig,
    manifest_path: Path,
    workspace: Path,
    *,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    clock: Clock | None = None,
    transports: IngestTransports = IngestTransports(),
    env: Mapping[str, str] | None = None,
    master_key: bytes | None = None,
) -> OnboardingStatus:
    """``onboard run`` (ADR-0017): every onboarding step, resumable; writes ``onboarding.json``.

    A step that is done on unchanged inputs is skipped. A step that fails
    is recorded and the rest still run where they can, so one unreachable
    source doesn't hide everything else. The needs (what a person must do)
    are recomputed every time from the current state.
    """
    clock = clock or SystemClock()
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    previous = _previous_steps(workspace)
    records: dict[str, StepRecord] = {}
    now = clock.now().isoformat()

    audit = _recorder(config, db, master_key, clock)

    def record(step: Step, state: State, inputs: str, detail: str, on: str | None = None) -> None:
        records[step] = StepRecord(step, state, inputs, detail, now, on)
        audit(
            "step",
            "onboarding",
            {"step": step, "state": state, "on": on, "detail": detail[:300]},
        )

    def skip(step: Step, inputs: str) -> bool:
        if should_run(previous.get(step), inputs):
            return False
        records[step] = previous[step]
        return True

    try:
        manifest = manifest_path.read_text(encoding="utf-8")
        inputs = digest(manifest)
        if not skip("ingest", inputs):
            try:
                reports = ingest_system(
                    config, manifest_path, workspace, transports=transports, env=env
                )
                missing = [r.source for r in reports if r.status == "unavailable"]
                if missing:
                    record(
                        "ingest",
                        "blocked",
                        inputs,
                        f"not read: {', '.join(missing)}",
                        on="config",
                    )
                else:
                    read = ", ".join(r.line().split(maxsplit=1)[-1] for r in reports)
                    hidden = [n for r in reports for n in r.notes if n.startswith("unreadable:")]
                    record("ingest", "done", inputs, "; ".join([read, *hidden]))
            except (ConfigError, ExternalServiceError) as exc:
                record("ingest", "failed", inputs, str(exc)[:200])
        if not (workspace / "sources").is_dir():
            return _autopilot_status(workspace, records, db, config, clock)

        sensed = _sense_digest(workspace, config)
        if not skip("sense", sensed):
            detail, waiting = _sense(config, workspace, provider, audit)
            sensed = _sense_digest(workspace, config)  # an install changed the plugins
            record(
                "sense",
                "blocked" if waiting else "done",
                sensed,
                detail,
                "user" if waiting else None,
            )

        stages: list[tuple[Step, Callable[[], str]]] = [
            (
                "graph",
                lambda: _graph_detail(
                    onboard_graph(config, workspace, provider=provider, engine=db, clock=clock)
                ),
            ),
            (
                "incidents",
                lambda: _incidents_detail(
                    onboard_incidents(config, workspace, provider=provider)[0]
                ),
            ),
            (
                "requests",
                lambda: _requests_detail(
                    onboard_requests(config, workspace, provider=provider, engine=db, clock=clock)[
                        0
                    ]
                ),
            ),
            (
                "evidence",
                lambda: _evidence_detail(
                    onboard_evidence(
                        config,
                        workspace,
                        provider=provider,
                        engine=db,
                        clock=clock,
                        master_key=master_key,
                    )
                ),
            ),
            (
                "readiness",
                lambda: _readiness_detail(
                    onboard_readiness(
                        config,
                        workspace,
                        provider=provider,
                        engine=db,
                        clock=clock,
                        master_key=master_key,
                    )[0]
                ),
            ),
            ("gaps", lambda: _gaps_detail(onboard_gaps(config, workspace, provider=provider))),
        ]
        for step, run in stages:
            # Readiness and gaps replay the evidence: a capture re-runs them, and so does
            # a live case resolving (WP-O6c: it calibrates a label, or proves a class).
            inputs = (
                digest(
                    sensed,
                    _tree_digest(workspace / "sources" / "evidence"),
                    _live_digest(db, config),
                )
                if step in ("readiness", "gaps")
                else digest(sensed, _STEP_VERSIONS[step])
                if step in _STEP_VERSIONS
                else sensed
            )
            if skip(step, inputs):
                continue
            try:
                detail = run()
            except (ConfigError, ExternalServiceError) as exc:
                record(step, "failed", inputs, str(exc)[:200])
                continue
            # The model gave no answer for part of it: re-running retries that part.
            unanswered = " unread" in detail
            record(
                step,
                "blocked" if unanswered else "done",
                inputs,
                detail,
                "model" if unanswered else None,
            )
        return _autopilot_status(workspace, records, db, config, clock)
    finally:
        if own:
            db.dispose()


#: A step whose work changed in this build: an upgrade re-runs it on unchanged sources
#: (WP-222: the graph step writes component summaries).
_STEP_VERSIONS: dict[str, str] = {"graph": "2"}


def _graph_detail(report: OnboardGraphReport) -> str:
    failed = f"; {len(report.failed_pages)} page(s) unread" if report.failed_pages else ""
    detail = f"{report.nodes} nodes, {report.edges} edges, {report.questions} question(s){failed}"
    run = report.summaries
    if run is not None and (run.summaries or run.unwritten):
        detail += f"; {len(run.summaries) - len(run.unwritten)} component summaries"
        detail += f" ({run.written} new)" if run.written else ""
        detail += f", {run.inferred} inferred fact(s)" if run.inferred else ""
        if run.unwritten:
            detail += f", {len(run.unwritten)} not written"
    if report.budget_spent:
        detail += "; the token budget (budget.llm_tokens) ran out: the next run continues"
    return detail


def _incidents_detail(mapped: IncidentMap) -> str:
    unread = f"; {len(mapped.unread)} unread" if mapped.unread else ""
    return f"{len(mapped.classes)} classes, {mapped.coverage:.0%} with a known cause{unread}"


def _requests_detail(plan: GapPlan) -> str:
    return f"{len(plan.requests)} request(s) unlocking {plan.unlocked:.0%}; {len(plan.gaps)} gap(s)"


def _evidence_detail(capture: EvidenceCapture | None) -> str:
    if capture is None:
        return (
            "no tool here reads the past (logs, metrics, deploys or CI under tools:): "
            "readiness replays the evidence already in the workspace"
        )
    return capture.line()


def _live_digest(engine: Engine, config: AppConfig) -> str:
    """WP-O6c: what readiness reads from the resolved live cases."""
    seen = LiveProof(uow=SqlUnitOfWork(engine), tenant_id=config.tenant.id).outcomes()
    return digest(*(f"{t}:{s.verdict}:{s.classification}" for t, s in sorted(seen.items())))


def _readiness_detail(report: ReadinessReport) -> str:
    return f"{report.ready_share:.0%} of the weight ready (bar {report.bar:.0%})"


def _autopilot_status(
    workspace: Path,
    records: Mapping[str, StepRecord],
    engine: Engine,
    config: AppConfig,
    clock: Clock,
) -> OnboardingStatus:
    """The steps, and what's needed from a person now (from the database and artifacts)."""
    tenant = config.tenant.id
    system = workspace.name
    manifest = workspace / "system.yaml"
    if manifest.is_file():
        system = load_system_manifest(manifest, {}, need_secrets=False).system.id
    status = OnboardingStatus(system=system, steps=[records[s] for s in STEPS if s in records])
    status.needs.extend(_step_needs(status.steps))
    status.needs.extend(_catalogue_needs(workspace))
    status.needs.extend(_gap_needs(workspace))
    review = GraphReview(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant).progress()
    pending = int(str(review["total"])) - int(str(review["reviewed"]))
    if pending:
        status.needs.append(
            Need("review", f"{pending} graph fact(s) to confirm or reject", 0.0, "/ui/onboarding")
        )
    if int(str(review["open_questions"])):
        status.needs.append(
            Need(
                "review",
                f"{review['open_questions']} question(s) to settle",
                0.0,
                "/ui/onboarding/questions",
            )
        )
    for req in RequestReview(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant).requests():
        name = f"{req.plugin}/{req.access} on {req.target}"
        if req.status == "open" and req.reopened_why:
            name += f": its credential was refused ({req.reopened_why})"
        elif req.status == "approved":  # ADR-0019: approved, its credential not bound yet
            name += f": approved, bind it with `supportagent onboard bind {req.id}`"
        if req.status in ("open", "approved"):
            status.needs.append(Need("access", name, req.share, "/ui/onboarding/requests"))
    report = ReadinessStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant).load()
    if report is not None:
        status.ready_share = float(str(report.get("ready_share", 0.0)))
        status.bar = float(str(report.get("bar", 0.0)))
        status.needs.extend(_readiness_needs(report))
    Workspace(workspace).write_artifact(AUTOPILOT_STATE, status.as_dict())
    document = status.as_dict() | {"at": clock.now().isoformat()}
    OnboardingStatusStore(uow=SqlUnitOfWork(engine), clock=clock, tenant_id=tenant).save(document)
    return status


def _step_needs(steps: Sequence[StepRecord]) -> list[Need]:
    """A step that failed, or waits on its config or the model, is a source to fix."""
    out = []
    for r in steps:
        if r.step == "ingest" and r.state == "done" and " unreadable" in r.detail:
            out.append(
                Need(
                    "access",
                    f"some sources can't be read by the instance ({r.detail})",
                    0.0,
                    "give the instance's user read access to them, or leave them out",
                )
            )
        if r.state == "failed" or (r.state == "blocked" and r.on == "config"):
            out.append(Need("source", f"{r.step}: {r.detail}", 0.0, "fix it, then re-run"))
        elif r.state == "blocked" and r.on == "model":
            out.append(Need("source", f"{r.step}: {r.detail}", 0.0, "check the model, then re-run"))
    return out


def _readiness_needs(report: Mapping[str, object]) -> list[Need]:
    """Classes that can't be ready yet: a plugin to provide, or evidence to capture."""
    out = []
    classes = report.get("classes")
    for c in classes if isinstance(classes, list) else []:
        if not isinstance(c, dict) or c.get("status") == "ready":
            continue
        reasons = "; ".join(str(x) for x in c.get("reasons", []))
        share = float(c.get("share", 0.0))
        what = f"{c.get('cause') or 'no cause'}: {reasons}"
        why = {str(u[1]) for u in c.get("unreplayed") or [] if isinstance(u, list) and len(u) == 2}  # noqa: PLR2004
        if c.get("status") == "untestable" and why == {"live_only"}:
            out.append(Need("evidence", what, share, "prove it in shadow (visible only live)"))
        elif c.get("status") == "untestable" and why == {"aged_out"}:
            out.append(Need("evidence", what, share, "the next incidents, or prove it in shadow"))
        elif c.get("status") == "untestable":
            out.append(Need("plugin", what, share, "add or evaluate the plugin"))
        elif c.get("no_evidence") and not c.get("scenarios"):
            out.append(
                Need("evidence", what, share, "supportagent onboard evidence (tools: configured)")
            )
        elif c.get("lab_status") == "ready":
            # WP-O6c: proven in the lab, held back in the pipeline
            out.append(
                Need(
                    "shadow",
                    what,
                    share,
                    "run in shadow: each resolved incident calibrates its label; then "
                    "supportagent onboard ingest --refresh and supportagent onboard run",
                )
            )
    gaps = report.get("catalog_gaps")
    for gap in gaps if isinstance(gaps, list) else []:
        if isinstance(gap, list) and len(gap) == 2:  # noqa: PLR2004 — (service, why)
            out.append(
                Need(
                    "config",
                    f"catalog: {gap[0]}: {gap[1]}",
                    0.0,
                    "supportagent onboard catalog drafts the entry: review it, add its paging",
                )
            )
    return out


# --- ADR-0017 (WP-A2): plugin catalogues and install ------------------------------------------


@dataclass(frozen=True, slots=True)
class Installed:
    plugin: str
    #: ``evaluated`` when its lab passed here, else ``skeleton``.
    status: str
    detail: str


def catalog_dirs(config: AppConfig, *, fetch: bool) -> list[tuple[str, Path | None, str]]:
    """Each configured catalogue as ``(label, directory, error)``.

    A git catalogue (ADR-0017 §2) is its verified checkout at the pinned commit:
    fetched now when ``fetch``, otherwise only if it's already in the cache.
    ``directory`` is ``None`` when there isn't one, and ``error`` says why.
    """
    cache = Path(config.plugins.cache_dir)
    out: list[tuple[str, Path | None, str]] = []
    for catalog in config.plugins.catalogs:
        if isinstance(catalog, str):
            out.append((catalog, Path(catalog), ""))
            continue
        try:
            checkout = (
                git_catalog.fetch(cache, catalog.git, catalog.ref)
                if fetch
                else git_catalog.cached(cache, catalog.git, catalog.ref)
            )
        except ConfigError as exc:
            out.append((catalog.label, None, str(exc)))
            continue
        if checkout is None:
            out.append((catalog.label, None, "not fetched yet: supportagent plugin fetch"))
        else:
            out.append((catalog.label, checkout / catalog.path, ""))
    return out


def fetch_catalogs(config: AppConfig) -> list[tuple[str, int | None, str]]:
    """``plugin fetch``: each catalogue fetched if needed, and how many plugins it offers."""
    out: list[tuple[str, int | None, str]] = []
    for label, directory, error in catalog_dirs(config, fetch=True):
        if directory is None:
            out.append((label, None, error))
            continue
        try:
            out.append((label, len(load_registry(directory)), ""))
        except (ConfigError, OSError) as exc:
            out.append((label, None, str(exc)))
    return out


def load_catalogs(config: AppConfig) -> list[LoadedPlugin]:
    """Every plugin the configured catalogues offer (each verified against its own index).

    A git catalogue is fetched when it isn't cached yet; one that can't be is a
    ``ConfigError``, like a directory that doesn't load.
    """
    out: list[LoadedPlugin] = []
    for label, directory, error in catalog_dirs(config, fetch=True):
        if directory is None:
            raise ConfigError(f"catalogue {label}: {error}")  # noqa: TRY003
        out.extend(load_registry(directory))
    return out


def install_from_catalog(  # noqa: PLR0913 — the plugin, the seams and who installs it
    config: AppConfig,
    plugin_id: str,
    *,
    provider: LLMProvider | None = None,
    root: Path | None = None,
    record: Recorder | None = None,
    actor: str = "operator",
) -> Installed:
    """``plugin install``: copy a catalogue plugin in, pin it, then prove it on its own lab.

    Its pack's lab runs with this instance's model (answers cached with the
    pack). If it fails the lab's gates here, the plugin is installed as a
    ``skeleton``: it can be detected and request access, but its classes
    can't be ready (ADR-0015 §2).
    """
    target_root = root if root is not None else plugins_root()
    offered = {p.manifest.id: p for p in load_catalogs(config)}
    if plugin_id not in offered:
        raise ConfigError(f"no configured catalogue offers {plugin_id!r}")  # noqa: TRY003
    directory = install_plugin(offered[plugin_id], target_root)
    _catalog_of.cache_clear()
    manifest = load_manifest(directory)
    pack = manifest.provides.pack
    if pack is None or not (directory / "pack" / "evals").is_dir():
        result = _settle(directory, target_root, "skeleton", "no lab to prove it on")
        if record is not None:
            record(
                "plugin_installed",
                actor,
                {"plugin": result.plugin, "version": manifest.version, "status": result.status},
            )
        return result
    scenarios, gates, evals = load_lab(config, pack)

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=evals / "recordings", snapshot_id=f"lab-{pack}")

    pipeline = LabPipeline(config, provider=provider, decorate=cache)
    report = run_lab(
        scenarios, pipeline, pack=pack, mode="install", model_id=pipeline.model_id, gates=gates
    )
    passed = sum(s.passed for s in report.scores)
    detail = f"its lab here: {passed}/{len(report.scores)} passed, " + (
        "gates met"
        if report.passed
        else "gates not met: " + "; ".join(g.detail for g in report.gates if not g.ok)
    )
    result = _settle(directory, target_root, "evaluated" if report.passed else "skeleton", detail)
    if record is not None:
        record(
            "plugin_installed",
            actor,
            {
                "plugin": result.plugin,
                "version": manifest.version,
                "publisher": manifest.publisher,
                "status": result.status,
                "lab": detail,
            },
        )
    return result


def _settle(directory: Path, root: Path, status: str, detail: str) -> Installed:
    """Record the status the lab earned in the installed manifest, and re-pin it."""
    path = directory / "plugin.yaml"
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("status:")]
    at = next(i for i, line in enumerate(lines) if line.startswith("core_api:")) + 1
    lines.insert(at, f"status: {status}   # ADR-0017: earned by its lab on this instance")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pin_one(root, directory.name)
    _catalog_of.cache_clear()
    return Installed(directory.name, status, detail)


def _sense_digest(workspace: Path, config: AppConfig) -> str:
    """What sensing reads: the sources, the installed plugins and every catalogue."""
    indexes = [
        plugins_root() / "index.yaml",
        *(d / "index.yaml" for _, d, _ in catalog_dirs(config, fetch=False) if d is not None),
    ]
    sources = [
        (path, sha)
        for path, sha in _tree_digest(workspace / "sources")
        if not path.startswith("evidence/")
    ]  # ADR-0018: evidence is the evidence step's output; readiness keys on it
    return digest(
        sources,
        [(str(i), hashlib.sha256(i.read_bytes()).hexdigest()) for i in indexes if i.is_file()],
        # a git catalogue's pin, fetched or not: a new commit re-runs sensing
        [(f"catalogue:{label}", "") for label, _, _ in catalog_dirs(config, fetch=False)],
    )


def _sense(
    config: AppConfig,
    workspace: Path,
    provider: LLMProvider | None,
    record: Recorder | None = None,
) -> tuple[str, list[str]]:
    """Detect over the installed plugins and every catalogue; install what may be.

    Returns the step's detail, and the catalogue plugins waiting for approval.
    """
    installed = {p.manifest.id for p in load_plugins()}
    offered = [p for p in load_catalogs(config) if p.manifest.id not in installed]
    found = [d for d in detect_plugins(workspace, offered) if d.detected] if offered else []
    publisher = {p.manifest.id: p.manifest.publisher for p in offered}
    done: list[str] = []
    waiting: list[str] = []
    for d in found:
        if config.plugins.auto_install == "first-party" and publisher[d.plugin] == "first-party":
            result = install_from_catalog(
                config, d.plugin, provider=provider, record=record, actor="auto"
            )
            done.append(f"{result.plugin} ({result.status}: {result.detail})")
        else:
            waiting.append(d.plugin)
    detections = detect_plugins(workspace, load_plugins())
    chosen = detected_ids(detections)
    Workspace(workspace).write_artifact(
        "detections.json",
        {
            "plugins": chosen,
            "why": {d.plugin: d.explain() for d in detections if d.detected},
            "available": [
                {"plugin": d.plugin, "why": d.explain(), "publisher": publisher[d.plugin]}
                for d in found
                if d.plugin in waiting
            ],
        },
    )
    parts = [f"plugins: {', '.join(chosen) or 'none'}"]
    if done:
        parts.append(f"installed {', '.join(done)}")
    if waiting:
        parts.append(f"{len(waiting)} in a catalogue awaiting approval: {', '.join(waiting)}")
    return "; ".join(parts), waiting


def _catalogue_needs(workspace: Path) -> list[Need]:
    """Detected plugins a catalogue offers that aren't installed yet: approve each one."""
    path = workspace / "detections.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    installed = {p.manifest.id for p in load_plugins()}
    return [
        Need(
            "plugin",
            f"install {a['plugin']} ({a['publisher']}) from the catalogue: {a['why']}",
            0.0,
            f"supportagent plugin install {a['plugin']}",
        )
        for a in raw.get("available", [])
        if isinstance(a, dict) and a.get("plugin") not in installed
    ]


# --- ADR-0017 (WP-A3): sensing the unknown ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogDraftEntry:
    """One service in a proposed catalog: ``new`` from the graph, or ``kept`` as configured."""

    entry: ServiceEntry
    status: Literal["new", "kept"]
    #: What a person still has to do for it (a paging integration, a stack).
    todo: tuple[str, ...] = ()


_SERVING_KINDS = frozenset({"Service", "Job", "UI"})
#: CMDB names (``CI1001``, ``Payments API``) to catalog names (``ci1001``, ``payments-api``).
_SLUG = re.compile(r"[^a-z0-9-]+")


def _catalog_name(raw: str) -> str:
    name = _SLUG.sub("-", raw.lower()).strip("-")[:63]
    return name if len(name) >= 2 and name[0].isalnum() else f"svc-{name}".strip("-")[:63]  # noqa: PLR2004


def draft_catalog(
    graph: SystemGraph, incident_cis: Sequence[str], configured: Sequence[ServiceEntry]
) -> list[CatalogDraftEntry]:
    """WP-O6c follow-up (ADR-0020 §5): the catalog onboarding implies, for review.

    Every serving component in the graph and every CI the history names becomes
    an entry with the stack, databases and namespace the graph gives it. A
    service the configured catalog already has (by name or CMDB CI) keeps its
    entry. Onboarding can't know a service's paging integration, and triage
    refuses a service without one (ARCH §14), so each entry missing it says so.
    """
    serving = {target_name(n.id, n.tech) for n in graph.nodes.values() if n.kind in _SERVING_KINDS}
    cis = set(incident_cis)
    out: dict[str, CatalogDraftEntry] = {}
    for raw in sorted(serving | cis):
        known = next((e for e in configured if e.name == raw or raw in e.cmdb_ci), None)
        if known is not None:
            todo: tuple[str, ...] = (
                () if known.paging_integration else ("add its paging_integration",)
            )
            if not known.stack:
                todo += ("add its stack (it selects the packs)",)
            out.setdefault(known.name, CatalogDraftEntry(known, "kept", todo))
            continue
        implied = service_entry(graph, raw)
        entry = ServiceEntry.model_validate(
            implied
            | {
                "name": _catalog_name(raw),
                "cmdb_ci": [raw] if raw in cis else [],
                "resources": [raw],
            }
        )
        todo = ("add its paging_integration",) + (
            () if entry.stack else ("add its stack (it selects the packs)",)
        )
        prior = out.get(entry.name)
        if prior is not None:  # two CMDB names for one service
            merged = sorted({*prior.entry.cmdb_ci, *entry.cmdb_ci})
            entry = prior.entry.model_copy(update={"cmdb_ci": merged})
        out[entry.name] = CatalogDraftEntry(entry, "new", todo)
    return [out[name] for name in sorted(out)]


def render_catalog_draft(entries: Sequence[CatalogDraftEntry], *, system: str) -> str:
    """``catalog.proposed.yaml``: a catalog that loads, each entry annotated with what's left."""
    lines = [
        f"# The service catalog onboarding proposes for {system} (ADR-0020 §5).",
        "# Review it, fill in each TODO, then copy it over the catalog paths.catalog names",
        "# (catalog/services.yaml, or local/catalog.yaml in a local install). Triage",
        "# refuses a service with no paging_integration (pagerduty:<service id> or",
        "# opsgenie:<team>), so fill those first.",
        "version: onboarding-proposed",
        "services:",
    ]
    for item in entries:
        lines.append(
            f"  # {item.status}" + (f"; TODO: {'; '.join(item.todo)}" if item.todo else "")
        )
        body = item.entry.model_dump(mode="json", exclude_defaults=True)
        dumped = dump_yaml([body]).splitlines()
        lines += [f"  {line}" for line in dumped]
    return "\n".join(lines) + "\n"


def onboard_catalog(
    config: AppConfig, workspace: Path, *, provider: LLMProvider | None = None
) -> tuple[list[CatalogDraftEntry], Path]:
    """``onboard catalog``: write ``catalog.proposed.yaml`` in the workspace, for review."""

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    plugins = load_plugins()
    run = run_onboarding_pipeline(
        workspace, plugins, OnboardingPipeline(config, provider=provider, decorate=cache)
    )
    try:
        configured = _build_catalog(config).services
    except (ConfigError, OSError):
        configured = []
    entries = draft_catalog(run.graph.graph, [i.ci for i in _past_incidents(workspace)], configured)
    path = workspace / "catalog.proposed.yaml"
    path.write_text(render_catalog_draft(entries, system=workspace.name), encoding="utf-8")
    if entries:  # what's written is a catalog that loads, once its TODOs are done
        Catalog.model_validate(load_yaml_document(path))
    return entries, path


def _signal_history(config: AppConfig) -> HttpSignalHistory:
    """The deployment's Prometheus and Loki, read-only, for measuring signals (WP-215)."""
    cfg = config.tools
    prom: httpx.Client | None = None
    loki: httpx.Client | None = None
    headers: dict[str, str] = {}
    if cfg.prometheus is not None:
        auth = (
            {"Authorization": f"Bearer {cfg.prometheus.token.get_secret_value()}"}
            if cfg.prometheus.token
            else {}
        )
        prom = httpx.Client(base_url=cfg.prometheus.base_url, headers=auth)
    if cfg.loki is not None:
        if cfg.loki.token:
            headers["Authorization"] = f"Bearer {cfg.loki.token.get_secret_value()}"
        if cfg.loki.org_id:
            headers["X-Scope-OrgID"] = cfg.loki.org_id
        loki = httpx.Client(base_url=cfg.loki.base_url)
    return signal_history(prom, loki, headers)


def onboard_detectors(  # noqa: PLR0913 — the workspace plus test seams
    config: AppConfig,
    workspace: Path,
    *,
    days: int = 7,
    history: SignalHistoryReader | None = None,
    now: datetime | None = None,
    provider: LLMProvider | None = None,
) -> tuple[list[SignalDetector], list[NotProposed], Path]:
    """``onboard detectors`` (WP-215): measure the signals, write the review file.

    Components come from the system graph onboarding built; thresholds from the
    deployment's own history. Every detector is written disabled: a person
    enables the ones they want, then ``apply_detectors`` renders those.
    """

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    run = run_onboarding_pipeline(
        workspace, load_plugins(), OnboardingPipeline(config, provider=provider, decorate=cache)
    )
    components = sorted(
        {
            target_name(n.id, n.tech)
            for n in run.graph.graph.nodes.values()
            if n.kind in _SERVING_KINDS
        }
    )
    reader = history if history is not None else _signal_history(config)
    signals = reader.measure(components, now=now or SystemClock().now(), days=days)
    drafts, skipped = draft_detectors(components, signals)
    path = write_review(workspace, drafts, skipped, system=workspace.name)
    return drafts, skipped, path


def apply_detectors(workspace: Path, rules_dir: Path) -> tuple[list[SignalDetector], list[Path]]:
    """``onboard detectors --apply``: the reviewed, enabled detectors as rule files."""
    detectors = with_enabled(read_drafts(workspace), read_enabled(workspace))
    return [d for d in detectors if d.enabled], write_rules(rules_dir, detectors)


def onboard_gaps(
    config: AppConfig, workspace: Path, *, provider: LLMProvider | None = None
) -> tuple[list[Gap], Path]:
    """``onboard gaps``: what no plugin covers in a workspace, with evidence; ``gaps.json``."""

    def cache(inner: LLMProvider) -> LLMProvider:
        return CachingLLMProvider(inner, directory=workspace / "recordings", snapshot_id="graph")

    plugins = load_plugins()
    pipeline = OnboardingPipeline(config, provider=provider, decorate=cache)
    run = run_onboarding_pipeline(workspace, plugins, pipeline)
    snapshot = snapshot_from_sources(workspace)
    cmdb_path = workspace / "sources" / "servicenow" / "cmdb.yaml"
    cmdb = load_yaml_document(cmdb_path) if cmdb_path.is_file() else {}
    declared = [(p.manifest.id, e) for p in plugins for e in p.manifest.provides.extractors]
    cis = [
        (str(c.get("name")), str(c.get("sys_class_name")), str(c.get("install_status", "")))
        for c in cmdb.get("cis") or []
        if isinstance(c, dict)
    ]
    packs = _pack_set(config)
    gaps = [
        *unclaimed_repos(snapshot.files, run.graph.graph),
        *unmapped_cmdb_classes(cis, mapped_cmdb_classes(declared)),
        *uncaused_incidents(run.incidents, _past_incidents(workspace)),
        *unknown_capabilities(
            {p.id: p.tools for p in packs.packs if not p.placeholder}, KNOWN_TOOL_SCHEMAS
        ),
    ]
    path = Workspace(workspace).write_artifact(
        "gaps.json", {"v": 1, "gaps": [g.as_dict() for g in gaps]}
    )
    return gaps, path


def _gaps_detail(result: tuple[list[Gap], Path]) -> str:
    gaps = result[0]
    if not gaps:
        return "nothing uncovered"
    kinds = Counter(g.kind for g in gaps)
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items()))


def _gap_needs(workspace: Path) -> list[Need]:
    path = workspace / "gaps.json"
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for g in raw.get("gaps", []):
        if not isinstance(g, dict):
            continue
        evidence = "; ".join(str(e) for e in g.get("evidence", []))
        what = f"{str(g.get('kind', '')).replace('_', ' ')} {g.get('subject')}: {evidence}"
        out.append(Need("plugin", what, float(g.get("weight", 0.0)), str(g.get("suggestion"))))
    return out


#: How a scaffold claims a repository from its manifest (``plugin new``), when the format allows.
_SCAFFOLD_RULES: Mapping[str, tuple[str, str, str]] = {
    "package.json": ("json", "^name$", "(?P<name>[A-Za-z0-9_.-]+)$"),
    "go.mod": ("lines", "^module$", "(?:.*/)?(?P<name>[A-Za-z0-9_.-]+)$"),
    "composer.json": ("json", "^name$", "(?:.*/)?(?P<name>[A-Za-z0-9_.-]+)$"),
}


def scaffold_plugin(plugin_id: str, manifest: str, out: Path) -> Path:
    """``plugin new`` (ADR-0017): a skeleton plugin from a gap, to review and complete.

    It detects the manifest and, where its format allows, claims the
    repository's component. It is written outside the registry: a person
    reviews it, adds causes and a pack, then puts it in a catalogue or
    ``plugins/`` and pins it.
    """
    target = out / plugin_id
    if target.exists():
        raise ConfigError(f"{target} already exists")  # noqa: TRY003
    tech = technology(manifest) or plugin_id
    rule = _SCAFFOLD_RULES.get(manifest)
    lines = [
        "# Scaffolded by `supportagent plugin new` (ADR-0017) from an onboarding gap.",
        "# Review it, add the causes this stack's incidents have (and a pack to test",
        "# them), then put it in a catalogue or plugins/ and pin it (`plugin index`).",
        f"id: {plugin_id}",
        "version: 0.1.0",
        f"description: TODO, what a {tech} system is and fails with.",
        'core_api: ">=1.0,<2"',
        "status: skeleton",
        "provides:",
        "  causes: []   # TODO: {id: <area>/<cause>, description: ...}",
    ]
    if rule is not None:
        fmt, key, value = rule
        lines += [
            "  extractors:",
            f'    - match: "**/{manifest}"',
            f"      format: {fmt}",
            "      rules:",
            f'        - {{key: "{key}", value: "{value}", own: true,'
            f' node: {{id: "service:{{name}}", kind: Service, tech: [{tech}]}}}}',
        ]
    else:
        lines.append(
            f"  # TODO: an extractor for {manifest} (json, yaml, env, properties or lines)"
        )
    lines += ["detectors:", "  signals:", f'    - {{files: "**/{manifest}", weight: 1.0}}']
    target.mkdir(parents=True)
    path = target / "plugin.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_manifest(target)  # it must load as written
    return path


# --- ADR-0017 (WP-A6): onboarding's audit trail ---------------------------------------------

Recorder = Callable[[str, str, Mapping[str, object]], None]


def _recorder(
    config: AppConfig, engine: Engine, master_key: bytes | None, clock: Clock
) -> Recorder:
    """``record(action, actor, detail)``: one ``onboarding`` event, committed on its own."""

    def record(action: str, actor: str, detail: Mapping[str, object]) -> None:
        uow = SqlUnitOfWork(engine)
        with uow:
            OnboardingAudit(
                events=_event_sink(uow, master_key, clock),
                ids=UlidIdGenerator(),
                clock=clock,
                tenant_id=config.tenant.id,
            ).record(action, actor=actor, detail=detail)  # type: ignore[arg-type]
            uow.commit()

    return record


def install_and_record(config: AppConfig, plugin_id: str) -> Installed:
    """``plugin install``: install from a catalogue, with the install in the audit trail."""
    engine = make_engine(config.database.url.get_secret_value())
    try:
        return install_from_catalog(
            config,
            plugin_id,
            record=_recorder(config, engine, None, SystemClock()),
            actor="operator",
        )
    finally:
        engine.dispose()


def toggle_plugin(config: AppConfig, plugin_id: str, *, disabled: bool) -> None:
    """``plugin disable`` / ``enable``: out of detection and packs, or back; audited."""
    set_disabled(plugins_root(), plugin_id, disabled=disabled)
    _catalog_of.cache_clear()
    engine = make_engine(config.database.url.get_secret_value())
    try:
        _recorder(config, engine, None, SystemClock())(
            "plugin_disabled" if disabled else "plugin_enabled", "operator", {"plugin": plugin_id}
        )
    finally:
        engine.dispose()


def onboard_cycle(  # noqa: PLR0913 — the inputs and the seams
    config: AppConfig,
    manifest_path: Path,
    workspace: Path,
    *,
    engine: Engine | None = None,
    master_key: bytes | None = None,
    provider: LLMProvider | None = None,
) -> tuple[OnboardingStatus, list[Finding]]:
    """``onboard watch``'s one cycle: ``onboard run``, then drift against the accepted snapshot.

    Drift findings go to the audit trail; the snapshot isn't accepted here: a
    person does that once they've dealt with them (``onboard drift --accept``).
    """
    own = engine is None
    db = engine if engine is not None else make_engine(config.database.url.get_secret_value())
    clock = SystemClock()
    try:
        status = onboard_run(
            config,
            manifest_path,
            workspace,
            provider=provider,
            engine=db,
            clock=clock,
            master_key=master_key,
        )
        findings, _, _ = onboard_drift(
            config,
            workspace,
            provider=provider,
            record=_recorder(config, db, master_key, clock),
        )
    finally:
        if own:
            db.dispose()
    return status, findings
