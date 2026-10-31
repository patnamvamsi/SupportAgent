"""``system.yaml``: the onboarding manifest (ADR-0015 §1, WP-O2).

It names a system and the sources that describe it. ServiceNow is read with
the app config's own connection and integration user, so the manifest scopes
it (assignment groups, history) but carries no ServiceNow credential.
Confluence and Git sources name the environment variables holding their
credentials (``*_env``), resolved by ``config.loader`` like every other
secret. ``access`` lists the read-only production access granted so far
(WP-O5 fills it); ``budget`` caps what one ingest may read.
"""

from __future__ import annotations

import re

from pydantic import Field, SecretStr, field_validator

from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.readiness import ReadinessGates

_ID = r"^[a-z0-9][a-z0-9-]{1,62}$"


class SystemId(DomainModel):
    id: str = Field(pattern=_ID)
    name: str = Field(min_length=1)
    owners: list[str] = Field(default_factory=list)


class ServiceNowScope(DomainModel):
    """Which ServiceNow records belong to the system."""

    business_service: str | None = None
    assignment_groups: list[str] = Field(min_length=1)
    #: How far back incidents are read: ``<n>mo`` (months).
    history: str = Field(default="12mo", pattern=r"^\d{1,2}mo$")
    include: list[str] = Field(
        default_factory=lambda: ["incident", "problem", "change_request", "cmdb_ci"]
    )

    @property
    def history_months(self) -> int:
        return int(self.history.removesuffix("mo"))


class ConfluenceSpace(DomainModel):
    space: str = Field(min_length=1)
    base_url: str | None = None  # https://<site>.atlassian.net/wiki; required to ingest
    user: SecretStr | None = None
    token: SecretStr | None = None


class GitSource(DomainModel):
    """``github:<owner>/<name glob>``, or ``file:<directory glob>`` for local checkouts."""

    repo: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)
    token: SecretStr | None = None

    @field_validator("repo")
    @classmethod
    def _known_host(cls, value: str) -> str:
        if not re.match(r"^(github:[^/\s]+/[^/\s]+|file:.+)$", value):
            raise ValueError(  # noqa: TRY003
                f"repo {value!r} must be github:<owner>/<name glob> or file:<directory glob>"
            )
        return value

    @property
    def host(self) -> str:
        return self.repo.split(":", 1)[0]


class Sources(DomainModel):
    servicenow: ServiceNowScope | None = None
    confluence: list[ConfluenceSpace] = Field(default_factory=list)
    git: list[GitSource] = Field(default_factory=list)


class Budget(DomainModel):
    llm_tokens: int = Field(default=20_000_000, gt=0)
    wall_clock: str = Field(default="6h", pattern=r"^\d+[hm]$")
    max_incidents: int = Field(default=5000, gt=0)
    max_pages: int = Field(default=1000, gt=0)
    max_files: int = Field(default=3000, gt=0)
    max_file_bytes: int = Field(default=262_144, gt=0)


class SystemManifest(DomainModel):
    system: SystemId
    sources: Sources
    #: Read-only production access granted so far (filled by WP-O5).
    access: list[dict[str, object]] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    #: WP-O6b: the readiness bar and per-class gates for this system.
    readiness: ReadinessGates = Field(default_factory=ReadinessGates)


__all__ = [
    "Budget",
    "ConfluenceSpace",
    "GitSource",
    "ServiceNowScope",
    "Sources",
    "SystemId",
    "SystemManifest",
]
