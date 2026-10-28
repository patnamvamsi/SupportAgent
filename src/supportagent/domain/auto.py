"""SOP auto mode (ADR-0014 part 2): a conditioned grant on one SOP step's scope.

Auto mode is a grant, not a bypass: a human approves a grant on part 1's
literal scope ``sop:<doc>@<hash>:<step>:<kind>``, and the grant carries
conditions the gate checks on every proposal it would cover:

- ``checks``: hypothesis ids whose tool checks must have run and held in
  this RCA (tool results, never doc text). At least one.
- ``services``: the services the grant applies to (empty: any).
- ``max_per_day``: at most this many actions on the scope in 24 hours.

Two rules hold for every SOP grant, whatever its conditions:

- the supported cause's claim passed the evidence check (``entail``);
- the kind declares a revert (``REVERTS``).

A condition that can't be evaluated (no evidence passed, an unknown or
malformed key, a confidence condition) fails closed: the proposal waits for a
human approval, and the gate records why. A confidence condition
(``min_score``) is refused until ADR-0003's thresholds are calibrated on
partner data: RCA's score today is a reporting share, not a calibrated one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import CUSTOMER_FACING_KINDS, ActionKind

#: How each auto-grantable kind is undone. Both are append-only internal
#: notes: the revert is a retraction note on the same record, proposed
#: through the gate like any other action.
REVERTS: Mapping[ActionKind, str] = {
    ActionKind.WORK_NOTE: "retraction_note",
    ActionKind.ENRICH_PAGE: "retraction_note",
}
SOP_SCOPE_PREFIX = "sop:"
MAX_PER_DAY_LIMIT = 50
_KEYS = frozenset({"checks", "services", "max_per_day"})


@dataclass(frozen=True, slots=True)
class AutoConditions:
    checks: tuple[str, ...]
    max_per_day: int
    services: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.checks:
            raise DomainValidationError("an auto grant names at least one check")  # noqa: TRY003
        if not 1 <= self.max_per_day <= MAX_PER_DAY_LIMIT:
            raise DomainValidationError(  # noqa: TRY003
                f"max_per_day must be between 1 and {MAX_PER_DAY_LIMIT}"
            )

    def to_json(self) -> str:
        body = {
            "checks": sorted(set(self.checks)),
            "max_per_day": self.max_per_day,
            "services": sorted(set(self.services)),
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> AutoConditions:
        """Parse a stored block; anything unexpected is a ``DomainValidationError``."""
        try:
            body = json.loads(raw)
        except ValueError:
            raise DomainValidationError("conditions are not JSON") from None  # noqa: TRY003
        if not isinstance(body, dict):
            raise DomainValidationError("conditions are not an object")  # noqa: TRY003
        if "min_score" in body:
            raise DomainValidationError(  # noqa: TRY003
                "a confidence condition waits for calibrated thresholds (ADR-0003)"
            )
        unknown = set(body) - _KEYS
        if unknown:
            raise DomainValidationError(f"unknown condition(s): {sorted(unknown)}")  # noqa: TRY003
        checks, services = body.get("checks"), body.get("services", [])
        cap = body.get("max_per_day")
        if not _strings(checks) or not _strings(services) or type(cap) is not int:
            raise DomainValidationError("malformed conditions")  # noqa: TRY003
        return cls(checks=tuple(checks), max_per_day=cap, services=tuple(services))


@dataclass(frozen=True, slots=True)
class AutoEvidence:
    """What the RCA job knows when it submits an SOP step: tool results only."""

    service: str
    #: Hypotheses whose checks ran and held in this RCA.
    positive: frozenset[str]
    #: The supported cause's claim passed ``entail``.
    confirmed: bool
    rca_artifact: str

    def as_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "positive": sorted(self.positive),
            "confirmed": self.confirmed,
            "rca_artifact": self.rca_artifact,
        }


def auto_refusals(
    kind: ActionKind,
    conditions: AutoConditions,
    evidence: AutoEvidence | None,
    *,
    used_today: int,
) -> list[str]:
    """Why a conditioned grant does not cover this proposal; empty when it does."""
    if kind in CUSTOMER_FACING_KINDS or kind not in REVERTS:
        return [f"{kind.value} declares no revert"]
    if evidence is None:
        return ["no evidence to evaluate the conditions"]
    out: list[str] = []
    missing = sorted(set(conditions.checks) - evidence.positive)
    if missing:
        out.append(f"checks not positive: {missing}")
    if not evidence.confirmed:
        out.append("the cause's claim did not pass the evidence check")
    if conditions.services and evidence.service not in conditions.services:
        out.append(f"service {evidence.service!r} not in {sorted(conditions.services)}")
    if used_today >= conditions.max_per_day:
        out.append(f"rate cap reached: {used_today} of {conditions.max_per_day} today")
    return out


def reason(exc: DomainValidationError) -> str:
    """The one-line rule a ``DomainValidationError`` names (for audit records and the CLI)."""
    errors = exc.errors()
    msg = str(errors[0]["msg"]) if errors else str(exc)
    return msg.removeprefix("Value error, ")


def _strings(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


__all__ = [
    "MAX_PER_DAY_LIMIT",
    "REVERTS",
    "SOP_SCOPE_PREFIX",
    "AutoConditions",
    "AutoEvidence",
    "auto_refusals",
    "reason",
]
