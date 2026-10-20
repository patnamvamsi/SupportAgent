"""The shadow → assisted promotion gate (CAP §4.7, ARCH §8.4; 11-wp WP-115).

The gate reads the **Wilson 95 % lower bound** of triage agreement, never the
raw rate. It must clear ``min_lower_bound`` (0.75, prov.) over at least
``min_cases`` (60, prov.) labelled shadow cases, and so must every
configured severity band (the P1-P2 band by default, CAP §4.7 "per severity
band for P1/P2"). For example, 80 % over 30 cases has a lower bound of about
0.63 and does not pass. Thresholds come from ``evals/gates.yaml`` and stay
provisional until ADR-0003 sets them from the v0.0 shadow run. Passing the
gate makes promotion *possible*; the partner decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from supportagent.domain.stats import wilson_lower_bound


@dataclass(frozen=True, slots=True)
class Threshold:
    min_lower_bound: float
    min_cases: int


@dataclass(frozen=True, slots=True)
class PromotionGate:
    overall: Threshold = Threshold(min_lower_bound=0.75, min_cases=60)
    bands: Mapping[str, Threshold] = field(
        default_factory=lambda: {"P1-P2": Threshold(min_lower_bound=0.75, min_cases=20)}
    )
    provisional: bool = True


@dataclass(frozen=True, slots=True)
class GateCheck:
    name: str
    n: int
    agreed: int
    lower_bound: float | None
    passed: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "n": self.n,
            "agreed": self.agreed,
            "lower_bound": self.lower_bound,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PromotionResult:
    passed: bool
    checks: list[GateCheck]
    provisional: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "provisional": self.provisional,
            "checks": [c.as_dict() for c in self.checks],
        }


def check(name: str, agreed: int, n: int, threshold: Threshold) -> GateCheck:
    if n < threshold.min_cases:
        return GateCheck(
            name,
            n,
            agreed,
            wilson_lower_bound(agreed, n) if n else None,
            False,
            f"{n} labelled cases; needs >= {threshold.min_cases}",
        )
    lb = wilson_lower_bound(agreed, n)
    ok = lb >= threshold.min_lower_bound
    verdict = ">=" if ok else "<"
    return GateCheck(
        name, n, agreed, lb, ok, f"lower bound {lb:.3f} {verdict} {threshold.min_lower_bound:.2f}"
    )


def evaluate(
    overall: tuple[int, int], bands: Mapping[str, tuple[int, int]], gate: PromotionGate
) -> PromotionResult:
    """``overall`` and each band are ``(agreed, n)``; every check must pass."""
    checks = [check("overall", overall[0], overall[1], gate.overall)]
    for band, threshold in sorted(gate.bands.items()):
        agreed, n = bands.get(band, (0, 0))
        checks.append(check(band, agreed, n, threshold))
    return PromotionResult(all(c.passed for c in checks), checks, gate.provisional)


__all__ = ["GateCheck", "PromotionGate", "PromotionResult", "Threshold", "check", "evaluate"]
