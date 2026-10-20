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
from supportagent.domain.webhook import validate_mapping

# 02 §1: model_id must be a dated snapshot; a floating alias has no date or
# version component, i.e. no digit at all.
_VERSION_COMPONENT = re.compile(r"\d")
# 02 §1 denylist: alias tokens that float even when the id carries digits
# (e.g. "claude-3-5-sonnet-latest", "gpt-4o-latest").
_FLOATING_ALIAS = re.compile(r"(^|[-_.:@/])(latest|current|default|stable)$", re.IGNORECASE)
# 02 §1 / CAP §4.7: v0.0 and v0.1 run shadow only; assisted lands with WP-209 (v0.2).
_MODES_BEFORE_V02: frozenset[str] = frozenset({"shadow"})
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


class Identity(DomainModel):
    oidc_issuer: str = Field(min_length=1)
    client_id: SecretStr
    client_secret: SecretStr
    redirect_url: str = Field(min_length=1)
    session_secret: SecretStr

    @field_validator("session_secret")
    @classmethod
    def _session_secret_at_least_32_bytes(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < MIN_SESSION_SECRET_BYTES:
            raise ValueError(  # noqa: TRY003
                f"session_secret must be at least {MIN_SESSION_SECRET_BYTES} bytes (02 §1)"
            )
        return value


class LLMSlot(DomainModel):
    model_id: str = Field(min_length=1)
    timeout_s: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)

    @field_validator("model_id")
    @classmethod
    def _no_floating_aliases(cls, value: str) -> str:
        if _VERSION_COMPONENT.search(value) is None or _FLOATING_ALIAS.search(value):
            raise ValueError(  # noqa: TRY003
                f"model_id '{value}' looks like a floating alias (no date or version "
                "component); pin a dated snapshot (02 §1, ARCH §9)"
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
    min_grade: Literal["smoke"]  # smoke (v0.0) | holdout (v0.1+)
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

    rca: Literal["none"]  # none | lite | deep (lite from v0.1)
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
    #: v0.1 (WP-108): case-memory ranking weights.
    memory: Memory = Field(default_factory=Memory)
    #: v0.1 (WP-113): generic webhook sources; none by default.
    webhooks: list[Webhook] = Field(default_factory=list)

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
                "(v0.0 accepts only 'shadow'; use --demo for demos); see 02 §1, CAP §4.7"
            )
        return self
