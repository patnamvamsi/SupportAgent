"""Comms plans and rendering (ARCH §6.3, CAP §4.1, 01 §7.4, §7.12; 11-wp WP-102).

A **plan** is approved once and then authorizes every cadence update that
fits it:

- **template:** the plan's ``template_id``;
- **fields:** every varying field is in ``allowed_fields``, each with its
  **source**, which is either ``record_state`` (read from the ServiceNow
  record *as last set by a human*, ARCH §8.2) or ``schedule`` (computed from
  the cadence). There is no ``agent`` source: nothing the agent inferred can
  auto-send to a customer (``test_plan_rejects_agent_source_field``).

``plan_hash`` covers audience, channel, template, fields and cadence (01
§7.1), so editing any of them voids the approval. The trigger re-checks
``approvals.payload_hash = comms_plans.plan_hash`` on every send.

``render`` substitutes ``{{ name }}`` placeholders, the subset of Jinja2
templates use here. A placeholder that isn't an allowed field is an error
(StrictUndefined semantics), and nothing else (logic, filters) is
interpreted, so a template can't compute a value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.hashing import plan_hash

FieldSource = Literal["record_state", "schedule"]
_SOURCES: frozenset[str] = frozenset({"record_state", "schedule"})
#: The schedule-sourced fields the cadence computes.
SCHEDULE_FIELDS = frozenset({"next_update_eta"})
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_NAME = re.compile(r"^[a-z_][a-z0-9_.]*$")
#: v0.1 plan channels: the trigger's plan path authorizes customer kinds only.
CHANNELS: Mapping[str, str] = {"servicenow_comment": "customer_comment"}


@dataclass(frozen=True, slots=True)
class AllowedField:
    name: str
    source: FieldSource

    def __post_init__(self) -> None:
        if self.source not in _SOURCES:
            raise DomainValidationError(  # noqa: TRY003
                f"field {self.name!r}: source must be record_state or schedule, "
                f"never {self.source!r} (ARCH §6.3)"
            )
        if not _NAME.match(self.name):
            raise DomainValidationError(f"bad field name {self.name!r}")  # noqa: TRY003
        if self.source == "schedule" and self.name not in SCHEDULE_FIELDS:
            raise DomainValidationError(  # noqa: TRY003
                f"field {self.name!r}: schedule fields are {sorted(SCHEDULE_FIELDS)}"
            )


@dataclass(frozen=True, slots=True)
class PlanSpec:
    audience: Literal["customer", "internal"]
    channel: str
    template_id: str
    allowed_fields: tuple[AllowedField, ...]
    cadence_minutes: int | None

    def __post_init__(self) -> None:
        if self.audience != "customer":
            raise DomainValidationError(  # noqa: TRY003
                "v0.1 comms plans are customer plans (BL-033: internal cadence)"
            )
        if self.channel not in CHANNELS:
            raise DomainValidationError(  # noqa: TRY003
                f"unknown channel {self.channel!r}; known: {sorted(CHANNELS)}"
            )
        if self.cadence_minutes is not None and self.cadence_minutes <= 0:
            raise DomainValidationError("cadence_minutes must be positive")  # noqa: TRY003
        names = [f.name for f in self.allowed_fields]
        if len(names) != len(set(names)):
            raise DomainValidationError("allowed fields must be unique")  # noqa: TRY003

    @property
    def fields(self) -> dict[str, str]:
        return {f.name: f.source for f in self.allowed_fields}

    @property
    def send_kind(self) -> str:
        return CHANNELS[self.channel]

    def payload(self) -> dict[str, object]:
        """The plan proposal's payload; its ``payload_hash`` *is* the ``plan_hash``."""
        return {
            "audience": self.audience,
            "channel": self.channel,
            "template_id": self.template_id,
            "allowed_fields": self.fields,
            "cadence_minutes": self.cadence_minutes,
        }

    def plan_hash(self) -> str:
        return plan_hash(
            audience=self.audience,
            channel=self.channel,
            template_id=self.template_id,
            allowed_fields=self.fields,
            cadence_minutes=self.cadence_minutes,
        )


def placeholders(template: str) -> list[str]:
    return [m.group(1) for m in _PLACEHOLDER.finditer(template)]


def check_template(template: str, allowed: Mapping[str, str]) -> None:
    """Every placeholder is an allowed field (checked at load, and again at render)."""
    unknown = sorted({p for p in placeholders(template) if p not in allowed})
    if unknown:
        raise DomainValidationError(  # noqa: TRY003
            f"template uses variables outside the plan's allowed fields: {', '.join(unknown)}"
        )


def render(template: str, fields: Mapping[str, str], allowed: Mapping[str, str]) -> str:
    """Fill ``{{ name }}`` from ``fields``; anything outside ``allowed`` or unset is an error."""
    check_template(template, allowed)
    missing = sorted({p for p in placeholders(template) if p not in fields})
    if missing:
        raise DomainValidationError(f"no value for {', '.join(missing)}")  # noqa: TRY003
    return _PLACEHOLDER.sub(lambda m: fields[m.group(1)], template)


def interval_slot(now: datetime, approved_at: datetime, cadence_minutes: int) -> int:
    """01 §7.12: ``floor((now - approved_at) / cadence)``; the first update is slot 0."""
    elapsed = (now - approved_at).total_seconds()
    return max(0, int(elapsed // (cadence_minutes * 60)))


__all__ = [
    "CHANNELS",
    "SCHEDULE_FIELDS",
    "AllowedField",
    "FieldSource",
    "PlanSpec",
    "check_template",
    "interval_slot",
    "placeholders",
    "render",
]
