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

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import anthropic
import openai
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.engine import make_engine
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.cases import CaseRepo
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.files.calibration_loader import load_calibration
from supportagent.adapters.files.catalog_loader import load_catalog
from supportagent.adapters.files.preclass_loader import load_preclass_rules
from supportagent.adapters.llm.client import LLMClient, SlotSpec, build_llm_client
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import Redactor
from supportagent.adapters.system.clock import SystemClock
from supportagent.adapters.system.ids import UlidIdGenerator
from supportagent.app.classify import Classifier
from supportagent.app.gate import GateService
from supportagent.app.replay import ReplayService, ReplayView
from supportagent.app.triage import TriageService
from supportagent.config.loader import load_master_key_default
from supportagent.config.schema import AppConfig, RoutingRow
from supportagent.domain.errors import ConfigError, DomainValidationError, ExternalServiceError
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.preclass import RuleSet
from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.slots import SlotName
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator

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


def _build_ruleset(config: AppConfig) -> RuleSet:
    """The pre-classifier rules beside the taxonomy (ARCH §4.2, 10-wp WP-010).

    An empty rule set would make every case abstain, so the cross-check never
    runs and every P1/P2 is capped — the shipped rules are always loaded.
    """
    return load_preclass_rules(Path(config.paths.taxonomy).parent / "preclass_rules.yaml")


def _build_llm(
    config: AppConfig,
    events: EventSink,
    ids: IdGenerator,
    clock: Clock,
) -> LLMClient:
    """The redacting LLM client for the configured provider (01 §5, INV-06)."""
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
    slot = config.llm.slots[SlotName.CLASSIFY.value]
    return build_llm_client(
        provider_name=provider,
        sdk_client=sdk_client,
        redactor=Redactor(detectors=built_in_detectors()),
        prompts=PromptRegistry(root=Path(config.paths.prompts)),
        events=events,
        ids=ids,
        clock=clock,
        slots={
            SlotName.CLASSIFY: SlotSpec(
                model_id=slot.model_id,
                timeout_s=slot.timeout_s,
                max_output_tokens=slot.max_output_tokens,
            )
        },
    )


def _build_triage(config: AppConfig) -> TriageService:
    """The full triage graph on one shared unit of work (10-wp WP-011)."""
    uow = _build_uow(config)
    clock = SystemClock()
    ids = UlidIdGenerator()
    master_key = load_master_key_default()
    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    events = SqlEventSink(uow=uow, cipher=aead)
    case_repo = CaseRepo(uow)
    catalog = _build_catalog(config)
    classifier = Classifier(
        rule_set=_build_ruleset(config),
        llm=_build_llm(config, events, ids, clock),
        calibration=_build_calibration(config),
        min_score=config.calibration.min_score,
        routing=_build_routing(config),
    )
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog,
        ttl=timedelta(hours=config.proposals.ttl_hours),
    )
    return TriageService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        case_repo=case_repo,
        classifier=classifier,
        gate=gate,
        catalog=catalog,
        tenant_constants=config.redaction.tenant_constants,
    )


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
        "TriageService": lambda: _build_triage(config),
    }


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
