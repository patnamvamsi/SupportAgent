"""WP-001: Severity + rank() (01 §4, §7.3: P1 > P2 > P3 > P4)."""

from __future__ import annotations

from supportagent.domain.severity import Severity, rank


def test_severity_rank_orders_p1_most_severe() -> None:
    ranks = [rank(severity) for severity in (Severity.P1, Severity.P2, Severity.P3, Severity.P4)]
    assert ranks == [1, 2, 3, 4]
    # "ranks above" (§7.3) means a smaller rank: P1 ranks above P4.
    assert rank(Severity.P1) < rank(Severity.P2) < rank(Severity.P3) < rank(Severity.P4)
