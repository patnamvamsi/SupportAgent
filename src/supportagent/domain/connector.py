"""The connector contract's domain half (ADR-0011 §3, WP-M07).

A **connector** is one incident system behind the ticket ports
(``ports.tickets``). This module holds what the core needs to know about it
without naming it:

- :class:`ConnectorManifest`: which :class:`Capability` s it has. A missing
  capability degrades the agent safely (:func:`limits_for`) and never fails
  open.
- :class:`TicketSemantics`: how its fields map onto the case. Which field
  holds the state, the assignee and the priority, which states end a case,
  how a priority maps to a severity and when a change raises it, and the
  display values customers see.
- :class:`ActorKind` and :func:`classify_actor`: who made a change. Our own
  integration identity, the system, a confirmed automation, or a human.

ServiceNow is the first connector (``adapters.servicenow``); a second one
waits until a customer needs it (14-wp WP-M07).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from supportagent.domain.severity import Severity


class Capability(StrEnum):
    """What a connector can do (its manifest lists the ones it has)."""

    POLL = "poll"  # changed records since a cursor
    RECORD = "record"  # one record's current fields
    HISTORY = "history"  # per-record field and note changes, with their actors
    INTERNAL_NOTES = "internal_notes"  # append a note the team only sees
    CUSTOMER_NOTES = "customer_notes"  # append a note the requester sees
    FIELD_UPDATES = "field_updates"  # set fields (read-compare first)
    MARKER_READ = "marker_read"  # find a note by its idempotency marker (INV-04)


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    name: str  # the coarse system name in refs and scopes ("servicenow")
    capabilities: frozenset[Capability]

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ConnectorLimits:
    """What the agent may do on this connector, and why it may not.

    - **takeover_detection** needs ``history``. Without it the agent can't see
      a human take a case over, so it never acts on its own: only modes where
      a person approves every action are allowed
      (``test_missing_history_capability_disables_auto_takeover``).
    - **writes** need ``marker_read``: without reconciliation a retried send
      could post twice (INV-04), so nothing is written at all.
    - **internal_notes**, **customer_notes**, **field_updates**: each action
      kind needs its capability.
    """

    takeover_detection: bool
    writes: bool
    internal_notes: bool
    customer_notes: bool
    field_updates: bool
    reasons: tuple[str, ...]

    @property
    def autonomous(self) -> bool:
        """May the agent act without a per-action human approval?"""
        return self.takeover_detection and self.writes


def limits_for(manifest: ConnectorManifest) -> ConnectorLimits:
    has = manifest.has
    reasons: list[str] = []
    for needed in (Capability.POLL, Capability.RECORD):
        if not has(needed):
            reasons.append(f"no {needed.value}: the connector can't feed the sync service")
    takeover = has(Capability.HISTORY)
    if not takeover:
        reasons.append("no history: human takeover can't be detected, so nothing runs unattended")
    writes = has(Capability.MARKER_READ)
    if not writes:
        reasons.append("no marker_read: sends can't be reconciled (INV-04), so nothing is written")
    notes = {
        cap: writes and has(cap)
        for cap in (Capability.INTERNAL_NOTES, Capability.CUSTOMER_NOTES, Capability.FIELD_UPDATES)
    }
    reasons.extend(
        f"no {cap.value}: those actions are disabled"
        for cap, ok in notes.items()
        if writes and not ok
    )
    return ConnectorLimits(
        takeover_detection=takeover,
        writes=writes,
        internal_notes=notes[Capability.INTERNAL_NOTES],
        customer_notes=notes[Capability.CUSTOMER_NOTES],
        field_updates=notes[Capability.FIELD_UPDATES],
        reasons=tuple(reasons),
    )


class TicketSemantics(Protocol):
    """How one connector's records map onto the case (lifecycle and severity)."""

    @property
    def state_field(self) -> str: ...

    @property
    def assignee_field(self) -> str: ...

    @property
    def priority_field(self) -> str: ...

    @property
    def resolved_states(self) -> frozenset[str]:
        """The state values that end a case (BL-032: configurable per instance)."""
        ...

    def severity_for(self, priority: str | None) -> Severity | None:
        """ADR-0009: the severity a source priority means, or ``None``."""
        ...

    def priority_raised(self, old: str | None, new: str | None) -> bool:
        """BL-047: does a change from ``old`` to ``new`` raise the priority?"""
        ...

    def display(self, field: str, raw: str) -> str:
        """The value a customer sees for a record field (``"6"`` -> ``"Resolved"``)."""
        ...


class ActorKind(StrEnum):
    AGENT = "agent"  # our integration identity
    SYSTEM = "system"  # the platform itself
    AUTOMATION = "automation"  # a confirmed automation actor (ARCH §8.2)
    HUMAN = "human"


_SYSTEM_ACTORS = frozenset({"system", "guest"})


def classify_actor(
    actor: str, *, integration_user: str, automation_actors: Collection[str]
) -> ActorKind:
    """ARCH §8.2. An unconfirmed automation candidate still counts as human.

    That errs toward a false takeover (the agent stops acting), never toward
    acting over a human. An empty actor is the system's.
    """
    if not actor or actor in _SYSTEM_ACTORS:
        return ActorKind.SYSTEM
    if actor == integration_user:
        return ActorKind.AGENT
    if actor in automation_actors:
        return ActorKind.AUTOMATION
    return ActorKind.HUMAN


__all__ = [
    "ActorKind",
    "Capability",
    "ConnectorLimits",
    "ConnectorManifest",
    "TicketSemantics",
    "classify_actor",
    "limits_for",
]
