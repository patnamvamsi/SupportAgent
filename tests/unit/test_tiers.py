"""WP-001: trust classes vs tiers (01 §4, ARCH §12.2 rule)."""

from __future__ import annotations

from supportagent.domain.tiers import Tier, TrustClass, class_agrees_with_tier

# 01 §4 / 10-wp: read → T0 only; execute → T1 or T2; state-changing → T2 only.
_EXPECTED: dict[TrustClass, frozenset[Tier]] = {
    TrustClass.READ: frozenset({Tier.T0}),
    TrustClass.EXECUTE: frozenset({Tier.T1, Tier.T2}),
    TrustClass.STATE_CHANGING: frozenset({Tier.T2}),
}


def test_class_tier_agreement_matrix() -> None:
    for trust_class, allowed in _EXPECTED.items():
        for tier in Tier:  # all 3x4 combinations
            assert class_agrees_with_tier(trust_class, tier) is (tier in allowed)
    # T3 is the human-only tier: no trust class agrees with it.
    for trust_class in TrustClass:
        assert class_agrees_with_tier(trust_class, Tier.T3) is False
