"""Proving a credential is read-only before it is bound (ADR-0019, WP-O5b).

An access contract (``plugin.yaml`` ``requires``) names what a credential
must be able to read (``verify_can``) and every write it must be refused
(``verify_cannot``). A verifier runs both against the target with the
candidate credential. A probe never writes: a refusal is proven by what the
system says the credential may do, or by an operation that changes nothing.

- **allowed**: the read worked, or the system would let the write happen;
- **refused**: the system refused it;
- **error**: it couldn't be told (unreachable, an unknown probe). For a
  ``cannot`` probe that is not a refusal: the credential isn't bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from supportagent.domain.access_verify import ProbeKind, ProbeOutcome, ProbeResult


class AccessVerifier(Protocol):
    """One technology's probes. ``name`` is what a contract's ``verifier`` names."""

    @property
    def name(self) -> str: ...

    def verify(
        self,
        credential: str,
        target: str,
        *,
        can: Sequence[str],
        cannot: Sequence[str],
        timeout_s: float,
    ) -> list[ProbeResult]:
        """One result per probe, ``can`` first, in order."""
        ...


__all__ = ["AccessVerifier", "ProbeKind", "ProbeOutcome", "ProbeResult"]
