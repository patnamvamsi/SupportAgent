"""Diagnostic ports for ``supportagent doctor`` (02 §4.1, 10-wp WP-015).

Each port is the narrow read the doctor checks need, so checks stay pure
application code (no SQL, no HTTP). ``ServiceNowProbe`` deliberately has a
single read method and nothing that writes: ``doctor`` never writes to
ServiceNow by construction (``test_doctor_never_writes_to_servicenow``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProbeRows:
    """One Table API read: the rows (string values) and the response headers."""

    rows: list[dict[str, str]]
    headers: Mapping[str, str] = field(default_factory=dict)


class ServiceNowProbe(Protocol):
    """Read-only Table API access (02 §6). Raises ``ServiceNowError`` with ``status``."""

    def read(self, table: str, *, query: str, fields: Sequence[str], limit: int) -> ProbeRows: ...


class SchemaProbe(Protocol):
    """The database as migrated (03 §4)."""

    def current_revision(self) -> str | None:
        """The applied Alembic revision, or ``None`` before the first migration."""
        ...

    def head_revision(self) -> str:
        """The newest revision shipped with this build."""
        ...

    def foreign_keys_enforced(self) -> bool | None:
        """SQLite: ``PRAGMA foreign_keys`` on a fresh connection; ``None`` on other engines."""
        ...


class ModelProbe(Protocol):
    """A 1-token LLM call through the redacting client (02 §4.1 llm)."""

    def probe(self, model_id: str) -> str:
        """The model the provider resolved ``model_id`` to."""
        ...


class DiscoveryProbe(Protocol):
    """The identity provider's discovery document (02 §4.1 oidc)."""

    def discover(self) -> str:
        """The verified issuer."""
        ...


class AutomationActorStore(Protocol):
    """Candidate automation actors (ARCH §8.2); an admin confirms them later."""

    def add_candidates(self, tenant_id: str, actors: Sequence[tuple[str, str]]) -> int:
        """Insert ``(sys_user, kind)`` rows not already present; the count inserted."""
        ...

    def summary(self, tenant_id: str) -> tuple[int, int]:
        """``(candidates, confirmed)`` for the tenant."""
        ...


__all__ = [
    "AutomationActorStore",
    "DiscoveryProbe",
    "ModelProbe",
    "ProbeRows",
    "SchemaProbe",
    "ServiceNowProbe",
]
