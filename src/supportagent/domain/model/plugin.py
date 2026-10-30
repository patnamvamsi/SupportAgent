"""The plugin manifest (ADR-0015 §2, WP-O1): one model for everything pluggable.

A plugin is ``plugins/<id>/plugin.yaml``. It may provide any of:

- a **pack** (ADR-0011 §1), by id under ``paths.packs``: taxonomy, hypotheses,
  probes, runbooks, prompt fragments, lab scenarios;
- **capabilities**: read-only evidence, each with one result schema;
- **backends**: how a capability is served, a first-party adapter (signed,
  in-process) or an MCP server (out of process);
- **extractors**: what it can read off a system during onboarding (WP-O3);
- **detectors**: the signals that say a system needs it (§2: how an instance
  knows what to ask for);
- **requires**: one least-privilege access contract per backend, quoted
  verbatim in an onboarding request and verified at provision (WP-O5).

What a manifest may never do is fixed here, at load: add authority. Every
plugin is ``T0-read``; a third-party plugin can't ship in-process code; a
backend can only serve a capability the plugin declares; a contract that
grants writes doesn't load.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from supportagent.domain.model.base import DomainModel

#: The plugin API this core implements; a manifest's ``core_api`` must admit it.
CORE_API = (1, 0)
_ID = r"^[a-z0-9][a-z0-9-]{1,40}$"
_SEMVER = r"^\d+\.\d+\.\d+$"
_CAPABILITY = r"^[a-z0-9_]+(\.[a-z0-9_]+)+$"
_CORE_API = re.compile(r"^>=(\d+)\.(\d+),<(\d+)$")
#: Grant text that means more than read-only; a contract containing it doesn't load.
_WRITE_GRANT = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE\s+TABLE|TRUNCATE|GRANT\s+DBA|"
    r"ACCOUNTADMIN|SYSADMIN|SECURITYADMIN|OPERATE|cluster-admin|write|admin)\b",
    re.IGNORECASE,
)


class CapabilitySpec(DomainModel):
    description: str = Field(min_length=1)
    #: Path (relative to the plugin) of the result's JSON schema; ``None`` until the adapter lands.
    schema_path: str | None = None


class McpBackend(DomainModel):
    image: str = Field(min_length=1)
    tools: list[str] = Field(min_length=1)


class Backend(DomainModel):
    """One way to serve the capabilities matching ``capability`` (a glob such as ``oracle.*``)."""

    capability: str = Field(min_length=1)
    first_party: str | None = None
    mcp: McpBackend | None = None
    #: The ``requires`` contract this backend's credential satisfies.
    access: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Backend:
        if (self.first_party is None) == (self.mcp is None):
            raise ValueError("a backend is either first_party or mcp")  # noqa: TRY003
        return self


class Extractor(DomainModel):
    """A discovery rule for WP-O3: a file glob or CMDB class, and what it yields."""

    match: str = Field(min_length=1)
    extract: str = Field(min_length=1)


class Signal(DomainModel):
    """One piece of evidence a system needs the plugin. Exactly one kind is set.

    - ``files`` (+ ``contains``): a repository file matching the glob (and the regex);
    - ``incidents`` (+ ``min_share``): the share of incidents whose text matches;
    - ``docs`` (+ ``min_pages``): current pages (not labelled legacy or draft) that
      mention the regex;
    - ``cmdb_class``: an installed CI of that class;
    - ``git_host``: the manifest's repositories live there;
    - ``changes_caused`` : at least this share of incidents was caused by a change.
    """

    files: str | None = None
    contains: str | None = None
    incidents: str | None = None
    min_share: float = Field(default=0.05, gt=0, le=1)
    docs: str | None = None
    min_pages: int = Field(default=2, ge=1)
    cmdb_class: str | None = None
    git_host: str | None = None
    changes_caused: float | None = Field(default=None, gt=0, le=1)
    weight: float = Field(default=1.0, gt=0, le=1)

    @model_validator(mode="after")
    def _one_kind(self) -> Signal:
        kinds = [
            self.files,
            self.incidents,
            self.docs,
            self.cmdb_class,
            self.git_host,
            self.changes_caused,
        ]
        if sum(k is not None for k in kinds) != 1:
            raise ValueError("a signal has exactly one kind")  # noqa: TRY003
        if self.contains is not None and self.files is None:
            raise ValueError("contains needs files")  # noqa: TRY003
        for pattern in (self.contains, self.incidents, self.docs):
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"bad regex {pattern!r}: {exc}") from exc  # noqa: TRY003
        return self

    @property
    def kind(self) -> str:
        for name in ("files", "incidents", "docs", "cmdb_class", "git_host", "changes_caused"):
            if getattr(self, name) is not None:
                return name
        raise AssertionError("validated")  # pragma: no cover


class Detectors(DomainModel):
    signals: list[Signal] = Field(min_length=1)
    #: The summed weight of fired signals that selects the plugin.
    min_score: float = Field(default=1.0, gt=0)


class AccessContract(DomainModel):
    """The least-privilege grant a backend needs, quoted verbatim in a request."""

    description: str = Field(min_length=1)
    grant: str = Field(min_length=1)
    #: The kind of system entity the access is for (one request per entity).
    target: Literal["Database", "Runtime", "Cluster", "Repository", "Observability", "Account"]
    #: Probes run at provision (WP-O5): what must work, and what must be refused.
    verify_can: list[str] = Field(min_length=1)
    verify_cannot: list[str] = Field(min_length=1)

    @field_validator("grant")
    @classmethod
    def _read_only(cls, value: str) -> str:
        match = _WRITE_GRANT.search(value)
        if match is not None:
            raise ValueError(  # noqa: TRY003
                f"grant {value!r} asks for more than read-only ({match.group(0)!r})"
            )
        return value


class Provides(DomainModel):
    pack: str | None = Field(default=None, pattern=_ID)
    capabilities: dict[str, CapabilitySpec] = Field(default_factory=dict)
    backends: list[Backend] = Field(default_factory=list)
    extractors: list[Extractor] = Field(default_factory=list)
    #: Capabilities other plugins provide that this one's knowledge uses.
    needs: list[str] = Field(default_factory=list)

    @field_validator("capabilities")
    @classmethod
    def _capability_names(cls, value: dict[str, CapabilitySpec]) -> dict[str, CapabilitySpec]:
        bad = [name for name in value if not re.match(_CAPABILITY, name)]
        if bad:
            raise ValueError(f"capability names must look like a.b: {bad}")  # noqa: TRY003
        return value


class PluginManifest(DomainModel):
    """One ``plugins/<id>/plugin.yaml``."""

    id: str = Field(pattern=_ID)
    version: str = Field(pattern=_SEMVER)
    description: str = Field(min_length=1)
    core_api: str
    publisher: Literal["first-party", "third-party"] = "first-party"
    #: A skeleton declares its contracts but has no evaluated knowledge yet:
    #: it can be detected and requested, never leave shadow mode.
    status: Literal["skeleton", "evaluated"] = "skeleton"
    provides: Provides = Field(default_factory=Provides)
    detectors: Detectors
    requires: dict[str, AccessContract] = Field(default_factory=dict)
    trust: Literal["T0-read"] = "T0-read"

    @field_validator("core_api")
    @classmethod
    def _admits_this_core(cls, value: str) -> str:
        match = _CORE_API.match(value.replace(" ", ""))
        if match is None:
            raise ValueError(f"core_api {value!r} must look like '>=1.0,<2'")  # noqa: TRY003
        low = (int(match.group(1)), int(match.group(2)))
        if not low <= CORE_API < (int(match.group(3)), 0):
            raise ValueError(  # noqa: TRY003
                f"core_api {value!r} doesn't admit this core ({CORE_API[0]}.{CORE_API[1]})"
            )
        return value

    @model_validator(mode="after")
    def _no_added_authority(self) -> PluginManifest:
        backends = self.provides.backends
        if self.publisher == "third-party" and any(b.first_party for b in backends):
            raise ValueError(  # noqa: TRY003
                "a third-party plugin ships MCP servers or data only, never in-process code"
            )
        declared = list(self.provides.capabilities)
        for backend in backends:
            if not any(_glob(backend.capability, c) for c in declared):
                raise ValueError(  # noqa: TRY003
                    f"backend for {backend.capability!r} serves no declared capability"
                )
            if backend.access is not None and backend.access not in self.requires:
                raise ValueError(f"backend access {backend.access!r} is not in requires")  # noqa: TRY003
        return self


def _glob(pattern: str, name: str) -> bool:
    return name == pattern or (pattern.endswith(".*") and name.startswith(pattern[:-1]))


__all__ = [
    "CORE_API",
    "AccessContract",
    "Backend",
    "CapabilitySpec",
    "Detectors",
    "Extractor",
    "McpBackend",
    "PluginManifest",
    "Provides",
    "Signal",
]
