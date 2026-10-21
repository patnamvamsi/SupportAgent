"""RCA-lite reduction (ARCH §5, §5.4, §5.5; 11-wp WP-110): categorical, evidence-only.

``reduce`` is the ARCH §5 truth table, evaluated in this order:

| Condition | Outcome |
|---|---|
| >= 2 ``ok`` steps whose hypotheses ``excludes`` each other | INCONCLUSIVE (conflict) |
| >= 1 ``ok`` step | SUPPORTED (the first in plan order) |
| plan completed, every step ran, all not ``ok`` | CONSISTENT_UNCONFIRMED |
| anything else (budget, a failed tool call, an incomplete plan) | INCONCLUSIVE |

Entailment (§5.4) runs *after* this, on claims: it never changes the outcome.
A claim is only eligible to be shown in comms when entailment says
``supports``. ``unknown`` or a failure tags it ``speculation``, and
``contradicts`` tags it ``contradicted`` (shown to the human, never sent).

**Prior (§5.5):** a weight per hypothesis orders an LLM-proposed plan. It
never gates. A change freeze zeroes ``recent_change_push``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

Outcome = Literal["SUPPORTED", "CONSISTENT_UNCONFIRMED", "INCONCLUSIVE"]
Verdict = Literal["supports", "contradicts", "unknown"]
CHANGE_HYPOTHESIS = "recent_change_push"


@dataclass(frozen=True, slots=True)
class StepResult:
    step_id: str
    hypothesis: str
    ran: bool  # the tool call succeeded and the check was evaluated
    ok: bool
    why: str


@dataclass(frozen=True, slots=True)
class Reduction:
    outcome: Outcome
    reason: str
    supported_by: str | None = None  # the step id behind SUPPORTED


def reduce(
    results: Sequence[StepResult],
    *,
    plan_length: int,
    excludes: Mapping[str, Sequence[str]],
) -> Reduction:
    ok = [r for r in results if r.ran and r.ok]
    for i, a in enumerate(ok):
        for b in ok[i + 1 :]:
            if b.hypothesis in excludes.get(a.hypothesis, ()) or a.hypothesis in excludes.get(
                b.hypothesis, ()
            ):
                return Reduction(
                    "INCONCLUSIVE",
                    f"conflicting signals: {a.hypothesis} ({a.step_id}) vs "
                    f"{b.hypothesis} ({b.step_id})",
                )
    if ok:
        first = ok[0]
        return Reduction("SUPPORTED", f"{first.hypothesis}: {first.why}", first.step_id)
    if len(results) == plan_length and plan_length > 0 and all(r.ran for r in results):
        return Reduction("CONSISTENT_UNCONFIRMED", "every step ran; no check held")
    if plan_length == 0:
        return Reduction("INCONCLUSIVE", "no testable plan")
    failed = [r.step_id for r in results if not r.ran]
    if failed:
        return Reduction("INCONCLUSIVE", f"step(s) could not run: {', '.join(failed)}")
    return Reduction("INCONCLUSIVE", f"plan incomplete: {len(results)}/{plan_length} steps")


@dataclass(slots=True)
class Claim:
    """One evidence-linked statement of the conclusion (never LLM free text)."""

    text: str
    cites: list[str]  # step ids
    tags: set[str] = field(default_factory=set)

    @property
    def sendable(self) -> bool:
        """Eligible for any send: entailment said ``supports`` (§5.4)."""
        return not (self.tags & {"speculation", "contradicted"})


def conclude(results: Sequence[StepResult]) -> list[Claim]:
    """One claim per satisfied check, citing its step; nothing without evidence."""
    return [
        Claim(text=f"{r.hypothesis}: {r.why}", cites=[r.step_id]) for r in results if r.ran and r.ok
    ]


def apply_verdict(claim: Claim, verdict: Verdict | None) -> None:
    """§5.4: anything but ``supports`` is speculation; ``contradicts`` is also flagged."""
    if verdict != "supports":
        claim.tags.add("speculation")
    if verdict == "contradicts":
        claim.tags.add("contradicted")


def order_by_prior(
    hypotheses: Sequence[str], prior: Mapping[str, float], *, freeze: bool
) -> tuple[list[str], bool]:
    """Order a proposed plan by prior weight (stable). ``(ordered, prior_zeroed)``."""
    weights = dict(prior)
    zeroed = freeze and weights.get(CHANGE_HYPOTHESIS, 0.0) > 0
    if freeze:
        weights[CHANGE_HYPOTHESIS] = 0.0
    ordered = sorted(dict.fromkeys(hypotheses), key=lambda h: -weights.get(h, 0.0))
    return ordered, zeroed


__all__ = [
    "CHANGE_HYPOTHESIS",
    "Claim",
    "Outcome",
    "Reduction",
    "StepResult",
    "Verdict",
    "apply_verdict",
    "conclude",
    "order_by_prior",
    "reduce",
]
