"""Lease port (03 §2.5): cooperative single-worker guard for scheduled jobs.

Every worker job runs under a DB lease (03 §2.5) so that at most one process
drains a given queue at a time. The lease is a thin, atomic ``acquire`` /
``release`` pair (03 §2.5); the adapter (``LeaseRepo``) is a single global
lock table with an atomic upsert.

This port is intentionally minimal: jobs only need to *try* to take the lease
(``acquire`` → ``bool``) and to *drop* it when done (``release``). Renewal /
expiry semantics live in the adapter, not here (03 §2.5).
"""

from __future__ import annotations

from typing import Protocol


class Lease(Protocol):
    """Cooperative single-worker guard (03 §2.5)."""

    def acquire(self, *, name: str, owner: str, ttl_s: int) -> bool:
        """Try to take the lease for ``name``.

        Returns ``True`` iff this call holds the lease afterwards (fresh
        acquisition, renewal by the current owner, or takeover of an expired
        lease); a live lease held by another owner returns ``False``.
        """
        ...

    def release(self, *, name: str, owner: str) -> None:
        """Release the lease if (and only if) this owner holds it."""
        ...
