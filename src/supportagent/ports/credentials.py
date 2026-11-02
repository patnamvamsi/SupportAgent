"""Where bound credentials live (ADR-0019, WP-O5b): a secret store, never the database.

A credential is stored only once its probes proved it read-only, keyed by the
access request it answers. The database keeps the request's status and the
probes' results, never the secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredCredential:
    #: The access request it answers (``request_id(plugin, access, target)``).
    request: str
    plugin: str
    access: str
    target: str
    #: What serves it: ``first_party:<adapter>`` or ``mcp:<image>``.
    backend: str
    bound_ts: int
    secret: str = field(repr=False)


class CredentialStore(Protocol):
    def put(self, credential: StoredCredential) -> None: ...

    def get(self, request: str) -> StoredCredential | None: ...

    def remove(self, request: str) -> bool:
        """Delete it; ``False`` if there was none."""
        ...

    def all(self) -> list[StoredCredential]: ...


__all__ = ["CredentialStore", "StoredCredential"]
