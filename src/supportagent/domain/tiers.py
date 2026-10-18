"""Trust classes and tiers, and when a class agrees with a tier (01 §4, ARCH §12)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class Tier(StrEnum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class TrustClass(StrEnum):
    READ = "read"
    EXECUTE = "execute"
    STATE_CHANGING = "state-changing"


# 01 §4: a read tool may be T0; an execute tool may be T1 or T2; a
# state-changing tool may only be T2. T3 never agrees (reserved).
_AGREEMENT: Mapping[TrustClass, frozenset[Tier]] = {
    TrustClass.READ: frozenset({Tier.T0}),
    TrustClass.EXECUTE: frozenset({Tier.T1, Tier.T2}),
    TrustClass.STATE_CHANGING: frozenset({Tier.T2}),
}


def class_agrees_with_tier(trust_class: TrustClass, tier: Tier) -> bool:
    """True iff the trust class may hold the tier (01 §4)."""
    return tier in _AGREEMENT[trust_class]
