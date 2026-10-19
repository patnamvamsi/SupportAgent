"""Shared detector primitives (01 §7.13).

Pure helpers only — no SDK, no framework. Kept separate from the concrete
detectors so they are trivially unit-testable and reusable by tenant
``CUSTOM`` detectors (01 §7.13).
"""

from __future__ import annotations

import math
from collections import Counter


def shannon_entropy(value: str) -> float:
    """Shannon entropy (bits/char) of ``value``; ``0.0`` for the empty string.

    Used by the generic API-key detector to separate high-entropy secrets from
    low-entropy literals (``aaaaaaaa...`` must not be flagged).
    """
    if not value:
        return 0.0
    n = len(value)
    counts = Counter(value)
    return -sum((count / n) * math.log2(count / n) for count in counts.values())


def luhn_valid(digits: str) -> bool:
    """True iff ``digits`` (a 13 to 19 digit string) passes the Luhn checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:  # noqa: PLR2004 — Luhn step: subtract 9 when a doubled digit exceeds 9
                d -= 9
        total += d
    return total % 10 == 0
