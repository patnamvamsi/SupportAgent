"""Statistical helpers for calibration (01 §7.6)."""

from __future__ import annotations

import math


def wilson_lower_bound(correct: int, n: int, z: float = 1.959963984540054) -> float:
    """Wilson score interval, lower bound (01 §7.6).

    ``correct``/``n`` are the number of correct classifications out of ``n``
    trials for one label row. With no trials (``n <= 0``) the evidence is
    empty, so the bound is 0.0 (fail closed). ``z`` is the two-sided z-score
    (95% by default).
    """
    if n <= 0:
        return 0.0
    p = correct / n
    z_sq = z * z
    denominator = 1.0 + z_sq / n
    centre = p + z_sq / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p)) / n + (z_sq / (4.0 * n * n)))
    return (centre - margin) / denominator
