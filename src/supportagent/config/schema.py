"""Resolved config v1 models (02 §1).

The file on disk names environment variables (``*_env`` keys); the loader
resolves them against the environment and validates the *resolved* dict
against these models, so every secret field is a ``SecretStr`` and renders
masked (02 §1). ``extra="forbid"`` everywhere: an unknown key is a startup
``ConfigError`` (02 §1).
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity
from supportagent.domain.tiers import Tier, TrustClass, class_agrees_with_tier
from supportagent.domain.webhook import validate_mapping

# 02 §1: model_id must be a dated snapshot; a floating alias has no date or
# version component, i.e. no digit at all.
_VERSION_COMPONENT = re.compile(r"\d")
# 02 §1 denylist: alias tokens that float even when the id carries digits
# (e.g. "claude-3-5-sonnet-latest", "gpt-4o-latest").
_FLOATING_ALIAS = re.compile(r"(^|[-_.:@/])(latest|current|default|stable)$", re.IGNORECASE)
# 02 §1 / CAP §4.7: v0.0 and v0.1 run shadow only; assisted lands with WP-209 (v0.2).
#: ADR-0008: v0.1 accepts shadow and assisted; standard is v0.2+.
_MODES_BEFORE_V02: frozenset[str] = frozenset({"shadow", "assisted"})
# 02 §1: identity.session_secret must be >= 32 bytes.
MIN_SESSION_SECRET_BYTES = 32


class Tenant(DomainModel):
    id: str = Field(pattern=r"^[a-z0-9-]{2,32}$")
    display_name: str = Field(min_length=1)


class Database(DomainModel):
    """Resolved: ``url`` holds the connection string (from ``url_env``)."""

    url: SecretStr


class OauthClientCredentials(DomainModel):
    """ServiceNow auth kind ``oauth_client_credentials`` (02 §1)."""

    kind: Literal["oauth_client_credentials"]
    client_id: SecretStr
    client_secret: SecretStr


class BasicAuth(DomainModel):
    """ServiceNow auth kind ``basic`` (02 §1)."""

    kind: Literal["basic"]
    user: SecretStr
    password: SecretStr


class ServiceNow(DomainModel):
    instance_url: str = Field(min_length=1)
    auth: Annotated[OauthClientCredentials | BasicAuth, Field(discriminator="kind")]
    integration_user: str = Field(min_length=1)
    tables: list[Literal["incident"]] = Field(min_length=1)  # v0.0: incident only
    poll_interval_s: int = Field(gt=0)
    overlap_s: int = Field(ge=0)
    page_size: int = Field(gt=0)
    qps_limit: int = Field(gt=0)
    #: BL-035: human-set ``category`` or ``category/subcategory`` (case-insensitive)
    #: -> taxonomy label, for outcome feedback and shadow agreement. Empty: the
    #: lower-cased ``category/subcategory`` is used as the label as-is.
    classification_map: dict[str, str] = Field(default_factory=dict)
    #: BL-032: ``state`` values that end a case; ServiceNow's incident defaults
    #: are 6 resolved, 7 closed, 8 canceled. Set it for an instance with custom states.
    resolved_states: list[str] = Field(default_factory=lambda: ["6", "7", "8"], min_length=1)

    @field_validator("classification_map")
    @classmethod
    def _normalize_map(cls, value: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, label in value.items():
            norm = "/".join(part.strip() for part in key.lower().split("/"))
            if not norm or norm.count("/") > 1 or "" in norm.split("/"):
                raise ValueError(  # noqa: TRY003
                    f"classification_map key {key!r} must be 'category' or 'category/subcategory'"
                )
            if norm in out:
                raise ValueError(f"classification_map key {key!r} is duplicated")  # noqa: TRY003
            out[norm] = label
        return out


class Identity(DomainModel):
    oidc_issuer: str = Field(min_length=1)
    client_id: SecretStr
    client_secret: SecretStr
    redirect_url: str = Field(min_length=1)
    session_secret: SecretStr
    #: Where the server itself reaches the IdP when that differs from the
    #: issuer the browser sees (a Keycloak container: the browser uses
    #: ``http://localhost:8880``, the app ``http://keycloak:8080``). Only the
    #: scheme and host of server-side requests change; the issuer checked in
    #: discovery and in the id token stays ``oidc_issuer``.
    backchannel_url: str | None = None

    @field_validator("session_secret")
    @classmethod
    def _session_secret_at_least_32_bytes(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < MIN_SESSION_SECRET_BYTES:
            raise ValueError(  # noqa: TRY003
                f"session_secret must be at least {MIN_SESSION_SECRET_BYTES} bytes (02 §1)"
            )
        return value


def is_floating_alias(model_id: str) -> bool:
    """02 §1: no date or version component, or an alias token such as ``latest``."""
    return _VERSION_COMPONENT.search(model_id) is None or bool(_FLOATING_ALIAS.search(model_id))


class LLMSlot(DomainModel):
    model_id: str = Field(min_length=1)
    timeout_s: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)

    @field_validator("model_id")
    @classmethod
    def _no_floating_aliases(cls, value: str) -> str:
        if is_floating_alias(value):
            raise ValueError(  # noqa: TRY003
                f"model_id '{value}' looks like a floating alias (no date or version "
                "component); pin a dated snapshot (02 §1, ARCH §9)"
            )
        return value


class OnboardingModel(DomainModel):
    """ADR-0015 decision 3: a frontier model for onboarding when triage runs on a local one.

    Reading a system's documentation and drafting its graph is where a
    frontier model earns its cost. When this block is absent, onboarding uses
    the triage model (``slots.classify``), local or not.
    """

    provider: Literal["anthropic", "openai"]
    api_key: SecretStr
    base_url: str | None = None
    model_id: str = Field(min_length=1)
    timeout_s: int = Field(default=60, gt=0)
    max_output_tokens: int = Field(default=4000, gt=0)

    @field_validator("model_id")
    @classmethod
    def _pinned(cls, value: str) -> str:
        if is_floating_alias(value):
            raise ValueError(  # noqa: TRY003
                f"model_id '{value}' looks like a floating alias; pin a dated snapshot (02 §1)"
            )
        return value


class LLM(DomainModel):
    provider: Literal["anthropic", "openai", "ollama", "fixture"]
    api_key: SecretStr | None
    base_url: str | None = None
    slots: dict[str, LLMSlot] = Field(min_length=1)
    daily_budget_tokens: int = Field(gt=0)
    #: The provider's model deprecation feed (11-wp WP-106; ARCH §9 runbook).
    deprecation_feed: str | None = None
    #: ADR-0015: the frontier model onboarding uses, if triage's is local.
    onboarding: OnboardingModel | None = None

    @model_validator(mode="after")
    def _provider_api_key_consistency(self) -> LLM:
        if "classify" not in self.slots:
            raise ValueError("llm.slots.classify is required (02 §1)")  # noqa: TRY003
        if self.provider in ("anthropic", "openai") and self.api_key is None:
            raise ValueError(  # noqa: TRY003
                f"llm.api_key_env is required for provider '{self.provider}'"
            )
        if self.provider in ("ollama", "fixture") and self.api_key is not None:
            raise ValueError(  # noqa: TRY003
                f"llm.api_key_env must be absent for provider '{self.provider}'"
            )
        return self


class RedactionPattern(DomainModel):
    name: str = Field(min_length=1)
    regex: str = Field(min_length=1)

    @field_validator("regex")
    @classmethod
    def _compilable(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"redaction regex does not compile: {exc}") from exc  # noqa: TRY003
        return value


class Redaction(DomainModel):
    custom_patterns: list[RedactionPattern] = Field(default_factory=list)
    tenant_constants: list[str] = Field(default_factory=list)


class Calibration(DomainModel):
    path: str = Field(min_length=1)
    min_grade: Literal["smoke", "holdout"]  # smoke (v0.0) | holdout (v0.1+, WP-105)
    min_score: float = Field(ge=0.0, le=1.0)


class Paths(DomainModel):
    taxonomy: str = Field(min_length=1)
    hypotheses: str = Field(min_length=1)
    catalog: str = Field(min_length=1)
    prompts: str = Field(min_length=1)
    runbooks: str = Field(min_length=1)
    #: v0.1 (WP-102): comms plan templates and message bodies.
    comms: str = "templates/comms/"
    #: v0.1 (WP-115): release and promotion gate thresholds.
    gates: str = "evals/gates.yaml"
    #: ADR-0011 (WP-M01): skill packs, ``<packs>/<id>/pack.yaml``; ``common`` is
    #: built from taxonomy/hypotheses/runbooks above and is always active.
    packs: str | None = None


class RoutingMatch(DomainModel):
    classification: str = Field(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    severity: list[Severity] = Field(min_length=1)

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severities(cls, value: object) -> object:
        # Strict models do not str-coerce enums; map names to members here.
        if isinstance(value, list):
            return [Severity(item) if isinstance(item, str) else item for item in value]
        return value


class RoutingRow(DomainModel):
    """A `routing:` row: `match` is required (02 §1)."""

    id: str = Field(min_length=1)
    match: RoutingMatch | None
    actions: list[ActionKind] = Field(min_length=1)

    @field_validator("actions", mode="before")
    @classmethod
    def _coerce_actions(cls, value: object) -> object:
        # Strict models do not str-coerce enums; map names to members here.
        if isinstance(value, list):
            return [ActionKind(item) if isinstance(item, str) else item for item in value]
        return value

    rca: Literal["none", "lite"]  # none | lite (v0.1, WP-110) | deep (v0.2)
    comms: str | None = None  # comms plan template id (v0.1)

    @model_validator(mode="after")
    def _match_required(self) -> RoutingRow:
        if self.match is None:
            raise ValueError("routing rows require a match clause (02 §1)")  # noqa: TRY003
        return self


class DefaultRow(RoutingRow):
    """The `default:` row: same shape except `match` is absent (02 §1)."""

    match: RoutingMatch | None = None

    @model_validator(mode="after")
    def _match_required(self) -> DefaultRow:
        # The default row intentionally has no match clause.
        return self


class GrantRequest(DomainModel):
    """`grants_request:` entry (v0.2 shape; accepted and stored in v0.0)."""

    action: str = Field(min_length=1)
    system: str = Field(min_length=1)
    target: str = Field(min_length=1)
    requested_expiry_days: int = Field(gt=0)


class Proposals(DomainModel):
    ttl_hours: int = Field(gt=0)


class Retention(DomainModel):
    events_days: int = Field(gt=0)
    comms_days: int = Field(gt=0)
    alerts_days: int = Field(gt=0)


class Webhook(DomainModel):
    """One generic webhook source (02 §2, 11-wp WP-113). ``secret`` from ``secret_env``."""

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")
    secret: SecretStr
    mapping: dict[str, str] = Field(min_length=1)

    @field_validator("secret")
    @classmethod
    def _secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < MIN_SESSION_SECRET_BYTES:
            raise ValueError(  # noqa: TRY003
                f"webhook secret must be at least {MIN_SESSION_SECRET_BYTES} bytes"
            )
        return value

    @model_validator(mode="after")
    def _mapping_is_valid(self) -> Webhook:
        try:
            validate_mapping(self.mapping)
        except DomainValidationError as exc:
            raise ValueError(f"webhook {self.source}: {exc}") from exc  # noqa: TRY003
        return self


class Memory(DomainModel):
    """Case-memory ranking weights (ARCH §5.2, WP-108; prov.)."""

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "signature_match": 0.40,
            "bm25": 0.20,
            "structured_match": 0.20,
            "graph_similarity": 0.10,
            "recency": 0.10,
        }
    )

    @field_validator("weights")
    @classmethod
    def _known_components(cls, value: dict[str, float]) -> dict[str, float]:
        known = {"signature_match", "bm25", "structured_match", "graph_similarity", "recency"}
        unknown = sorted(set(value) - known)
        if unknown:
            raise ValueError(f"unknown memory weight(s): {', '.join(unknown)}")  # noqa: TRY003
        if any(w < 0 for w in value.values()):
            raise ValueError("memory weights must be non-negative")  # noqa: TRY003
        return value


class ToolAirflow(DomainModel):
    base_url: str = Field(min_length=1)
    token: SecretStr | None = None


class ToolGitHub(DomainModel):
    base_url: str = "https://api.github.com"
    token: SecretStr | None = None


class ToolOnCall(DomainModel):
    pagerduty_base_url: str = "https://api.pagerduty.com"
    pagerduty_token: SecretStr | None = None
    opsgenie_base_url: str = "https://api.opsgenie.com"
    opsgenie_token: SecretStr | None = None


class ToolPostgresDatabase(DomainModel):
    """One read-only connection (WP-M04): a role with SELECT on the ``pg_stat_*`` views
    (Postgres) or the oracle plugin's ``oracle_ro`` grant (Oracle, WP-O6a)."""

    dsn: SecretStr


class ToolPostgres(DomainModel):
    """``postgres.*`` (WP-M04): catalog ``databases:`` names map to these connections."""

    databases: dict[str, ToolPostgresDatabase] = Field(min_length=1)
    statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)


class ToolOracle(DomainModel):
    """``oracle.*`` (WP-O6a): catalog ``databases:`` names map to these connections.

    Each DSN is the oracle plugin's ``oracle_ro`` account (``user/password@host/service``).
    """

    databases: dict[str, ToolPostgresDatabase] = Field(min_length=1)
    call_timeout_ms: int = Field(default=5000, ge=100, le=60000)


class ToolPrometheus(DomainModel):
    base_url: str = Field(min_length=1)
    token: SecretStr | None = None


class ToolLoki(DomainModel):
    """``logs.search`` (BL-050): Loki's HTTP API, read-only (``query_range`` only)."""

    base_url: str = Field(min_length=1)
    token: SecretStr | None = None
    #: Multi-tenant Loki: the ``X-Scope-OrgID`` to read as.
    org_id: str | None = None


class ToolKubernetes(DomainModel):
    """``k8s.pod_status`` and the rollout half of ``changes.recent`` (WP-M04): read-only."""

    api_url: str = Field(min_length=1)
    token: SecretStr
    verify_tls: bool = True
    ca_file: str | None = None


class ToolChanges(DomainModel):
    """``changes.recent`` (WP-M04): which configured sources feed it."""

    github: bool = True
    kubernetes: bool = True


class McpServer(DomainModel):
    """A registered MCP server (WP-M06): its declared tools and the trust it gets.

    Class and tier are the operator's, fixed here, never taken from the server.
    Read-only by default: until the action gate covers MCP tools, only
    ``read`` / T0 registrations load.
    """

    url: str = Field(pattern=r"^https?://")
    token: SecretStr | None = None
    #: The only tools the client will call on this server.
    tools: list[str] = Field(min_length=1)
    trust_class: TrustClass = TrustClass.READ
    tier: Tier = Tier.T0
    timeout_s: float = Field(default=15.0, gt=0, le=120)

    @field_validator("trust_class", mode="before")
    @classmethod
    def _coerce_class(cls, value: object) -> object:
        return TrustClass(value) if isinstance(value, str) else value

    @field_validator("tier", mode="before")
    @classmethod
    def _coerce_tier(cls, value: object) -> object:
        return Tier(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _read_only(self) -> McpServer:
        if not class_agrees_with_tier(self.trust_class, self.tier):
            raise ValueError(  # noqa: TRY003
                f"class {self.trust_class.value!r} can't hold tier {self.tier.value} (ARCH §7)"
            )
        if self.trust_class is not TrustClass.READ:
            raise ValueError(  # noqa: TRY003
                "only read MCP tools can be registered until the action gate covers MCP (WP-M06)"
            )
        return self


class ToolBinding(DomainModel):
    """A capability served by an MCP tool instead of the first-party adapter (WP-M06)."""

    mcp: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    #: How capability args and replies map to the tool's: ``passthrough`` or a named
    #: profile (``adapters.tools.mcp.PROFILES``).
    profile: str = "passthrough"
    #: Fixed tool arguments from the reviewed config (e.g. a datasource uid).
    args: dict[str, str | int | bool] = Field(default_factory=dict)


class Tools(DomainModel):
    """First-party read tools (ARCH §7, WP-107). A tool is registered when configured.

    WP-M06: ``bindings`` moves a capability onto a tool of a registered MCP
    server (``mcp_servers``); a binding replaces the first-party adapter.
    """

    logs_root: str | None = None
    cron_status_dir: str | None = None
    airflow: ToolAirflow | None = None
    github: ToolGitHub | None = None
    oncall: ToolOnCall | None = None
    #: WP-M04: the python-postgres pack's tools.
    postgres: ToolPostgres | None = None
    #: WP-O6a: the oracle plugin's tools.
    oracle: ToolOracle | None = None
    prometheus: ToolPrometheus | None = None
    #: BL-050: ``logs.search`` over Loki.
    loki: ToolLoki | None = None
    kubernetes: ToolKubernetes | None = None
    changes: ToolChanges | None = None
    #: WP-M06: registered MCP servers and the capabilities bound to their tools.
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    bindings: dict[str, ToolBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bindings_resolve(self) -> Tools:
        for capability, binding in self.bindings.items():
            server = self.mcp_servers.get(binding.mcp)
            if server is None:
                raise ValueError(  # noqa: TRY003
                    f"tools.bindings.{capability}: no mcp server {binding.mcp!r}"
                )
            if binding.tool not in server.tools:
                raise ValueError(  # noqa: TRY003
                    f"tools.bindings.{capability}: tool {binding.tool!r} is not declared "
                    f"for mcp server {binding.mcp!r}"
                )
        return self


class PagingPagerDuty(DomainModel):
    base_url: str = "https://api.pagerduty.com"
    token: SecretStr
    #: PagerDuty requires a ``From`` header (a valid user's email) to add notes.
    from_email: str = Field(min_length=3)


class PagingOpsgenie(DomainModel):
    base_url: str = "https://api.opsgenie.com"
    api_key: SecretStr


class Paging(DomainModel):
    """``enrich_page`` / page lookup (ARCH §14, WP-112). Attach-only: never create a page.

    A catalog ``paging_integration`` of ``pagerduty:<service id>`` or
    ``opsgenie:<team>`` uses the matching provider here. Without one, the
    Null Object answers: no page lookups, ``enrich_page`` a no-op, and
    ``doctor`` warns.
    """

    pagerduty: PagingPagerDuty | None = None
    opsgenie: PagingOpsgenie | None = None


class Rca(DomainModel):
    """RCA-lite (ARCH §5, §5.4, §5.5; WP-110). Every value is provisional."""

    #: Ordering weight per hypothesis id when no runbook matches; never gates (§5.5).
    prior: dict[str, float] = Field(default_factory=lambda: {"recent_change_push": 0.35})
    #: A change freeze is active: ``recent_change_push`` is zeroed and logged (§5.5).
    freeze: bool = False
    max_tool_calls: int = Field(default=5, ge=1, le=25)
    max_tokens: int = Field(default=3000, ge=0)
    max_hypotheses: int = Field(default=3, ge=1, le=10)
    #: §5.4: at most this many claims are entailment-checked; the rest are speculation.
    max_claims: int = Field(default=5, ge=0, le=20)
    entail_budget_s: float = Field(default=20.0, gt=0)

    @field_validator("prior")
    @classmethod
    def _non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        if any(w < 0 for w in value.values()):
            raise ValueError("rca prior weights must be non-negative")  # noqa: TRY003
        return value


class SeverityConfig(DomainModel):
    """ADR-0009: how a case's severity is decided.

    ``source_priority``: the source record's priority (ServiceNow ``priority``),
    the LLM's severity only when the record has none. A future severity
    calculator lands as a new strategy value.
    """

    strategy: Literal["source_priority"] = "source_priority"


_SPACE = r"^[a-z0-9][a-z0-9-]{1,62}$"


class GitMarkdownSource(DomainModel):
    """One knowledge source (WP-M08): a Git markdown folder, scoped as ``space``."""

    kind: Literal["git_markdown"]
    space: str = Field(pattern=_SPACE)
    path: str = Field(min_length=1)


class ConfluenceSource(DomainModel):
    """BL-051: one Confluence Cloud space, read-only, scoped as ``space``.

    ``user`` is the Atlassian account email and ``token`` its API token (from
    ``user_env`` / ``token_env``). Pages are read at startup; an unreachable
    site leaves the space empty at runtime and fails ``doctor``.
    """

    kind: Literal["confluence"]
    space: str = Field(pattern=_SPACE)
    base_url: str = Field(pattern=r"^https://")
    space_key: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1)
    token: str = Field(min_length=1)
    max_pages: int = Field(default=200, ge=1, le=2000)
    timeout_s: float = Field(default=15.0, gt=0, le=120)


KnowledgeSource = Annotated[GitMarkdownSource | ConfluenceSource, Field(discriminator="kind")]


class KnowledgeConfig(DomainModel):
    """Docs as RCA guidance (ADR-0011 §4): where they come from and how much to read."""

    sources: list[KnowledgeSource] = Field(default_factory=list)
    #: Chunks per search (one per doc).
    top_k: int = Field(default=3, ge=1, le=10)
    #: Planner context budget for the retrieved text.
    max_guidance_chars: int = Field(default=1500, ge=200, le=6000)
    #: Probes docs may add to a plan, beyond the runbook's or the planner's.
    max_suggested: int = Field(default=2, ge=0, le=5)
    stale_after_days: int = Field(default=365, ge=1)

    @model_validator(mode="after")
    def _spaces_unique(self) -> KnowledgeConfig:
        spaces = [s.space for s in self.sources]
        if len(spaces) != len(set(spaces)):
            raise ValueError("knowledge source spaces must be unique")  # noqa: TRY003
        return self


class PluginsConfig(DomainModel):
    """ADR-0017: where available plugins come from, and whether onboarding installs them.

    Each catalogue is a directory laid out like ``plugins/``, with its own pinned
    ``index.yaml``. ``auto_install: first-party`` installs a detected first-party
    plugin (after its lab runs here); otherwise each one waits for approval
    (``supportagent plugin install <id>``). Third-party plugins always wait.
    """

    catalogs: list[str] = Field(default_factory=list)
    auto_install: Literal["none", "first-party"] = "none"


class OnboardingConfig(DomainModel):
    """ADR-0017 (WP-A6): switches for what onboarding's results change at runtime.

    ``routing: report`` keeps readiness measured but stops it gating triage and
    holding back claims (a first week, or an incident). ``calibration: artifact``
    ignores the table fitted from the system's history.
    """

    routing: Literal["enforce", "report"] = "enforce"
    calibration: Literal["fitted", "artifact"] = "fitted"


class AppConfig(DomainModel):
    """Resolved `supportagent.yaml` v1 (02 §1)."""

    config_version: Literal[1]
    tenant: Tenant
    mode: Literal["shadow", "assisted", "standard"]
    database: Database
    servicenow: ServiceNow
    identity: Identity
    llm: LLM
    redaction: Redaction
    calibration: Calibration
    paths: Paths
    routing: list[RoutingRow] = Field(min_length=1)
    default: DefaultRow | None = None
    proposals: Proposals
    grants_request: list[GrantRequest] = Field(default_factory=list)
    retention: Retention
    #: v0.1 (WP-107): first-party read tools; none by default.
    tools: Tools = Field(default_factory=Tools)
    #: v0.1 (WP-108): case-memory ranking weights.
    memory: Memory = Field(default_factory=Memory)
    #: v0.1 (WP-113): generic webhook sources; none by default.
    webhooks: list[Webhook] = Field(default_factory=list)
    #: v0.1 (WP-110): RCA-lite budgets, prior and freeze.
    rca: Rca = Field(default_factory=Rca)
    #: v0.1 (WP-112): paging providers for enrich_page and page lookups.
    paging: Paging = Field(default_factory=Paging)
    #: ADR-0009: where severity comes from.
    severity: SeverityConfig = Field(default_factory=SeverityConfig)
    #: WP-M08: knowledge sources (docs as guidance, never evidence).
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    #: ADR-0017: plugin catalogues and install.
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    #: ADR-0017 (WP-A6): runtime switches for readiness routing and calibration.
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)

    @model_validator(mode="after")
    def _webhook_sources_unique(self) -> AppConfig:
        names = [w.source for w in self.webhooks]
        if len(names) != len(set(names)):
            raise ValueError("webhook sources must be unique")  # noqa: TRY003
        return self

    @model_validator(mode="after")
    def _mode_gate(self) -> AppConfig:
        if self.mode not in _MODES_BEFORE_V02:
            raise ValueError(  # noqa: TRY003
                f"mode '{self.mode}' is not accepted before v0.2 "
                "(v0.1 accepts 'shadow' and 'assisted', ADR-0008; use --demo for demos); "
                "see 02 §1, CAP §4.7"
            )
        return self
