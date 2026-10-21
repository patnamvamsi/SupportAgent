"""Takeover and lifecycle signals from ServiceNow changes (ARCH §8.1, §8.2; 01 §7.11).

Pure functions over the per-change history: no I/O.

**Human actor** (ARCH §8.2): not our integration user, not ``system``, and not
a *confirmed* automation actor. An unconfirmed candidate still counts as
human. That errs toward a false takeover, where the agent stops acting,
never toward acting over a human.

**Signals, in change order** (ARCH §8.2 narrow triggers; 01 §7.11):

- ``state`` set to a resolved value (``6`` resolved, ``7`` closed, ``8`` canceled):
  ``resolved``, whoever set it.
- ``state`` set to an open value on a resolved case: ``reopened``.
- ``assigned_to`` set to the integration user by a human on a ``human_owned``
  case: ``hand_back`` (ARCH §8.2 "re-assigning to the agent's integration identity").
- Any other human change of ``assigned_to`` or ``state``: ``human_takeover``.

A comment or work note by a human is not takeover. Each signal is applied to
the running status; one that isn't a legal transition from it (a takeover of
an already human-owned case) is dropped, so replaying a history is idempotent.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from supportagent.domain.lifecycle import TRANSITIONS, CaseEvent, CaseStatus
from supportagent.domain.model.ticket import ChangeRecord

#: ServiceNow incident states that end a case (01 §7.11 "resolved/closed").
RESOLVED_STATES = frozenset({"6", "7", "8"})
_NOT_HUMAN = frozenset({"system", "guest"})
_TAKEOVER_FIELDS = frozenset({"assigned_to", "state"})


def is_human_actor(
    actor: str, *, integration_user: str, automation_actors: Collection[str]
) -> bool:
    """ARCH §8.2: not us, not ``system``, not a confirmed automation actor."""
    return (
        bool(actor)
        and actor != integration_user
        and actor not in _NOT_HUMAN
        and actor not in automation_actors
    )


@dataclass(frozen=True, slots=True)
class LifecycleSignal:
    """One lifecycle event and the change that caused it."""

    event: CaseEvent
    change: ChangeRecord
    before: CaseStatus
    after: CaseStatus


def lifecycle_signals(  # noqa: PLR0913 — the change set plus the actor and state inputs
    changes: Sequence[ChangeRecord],
    status: CaseStatus,
    *,
    integration_user: str,
    integration_ids: Collection[str],
    automation_actors: Collection[str],
    resolved_states: Collection[str] = RESOLVED_STATES,
) -> list[LifecycleSignal]:
    """The legal lifecycle events in ``changes`` (time order), from ``status``.

    ``integration_ids`` are the values ``assigned_to`` takes when a ticket is
    assigned to the agent (its ``sys_id``, and its user name).
    ``resolved_states`` are the ``state`` values that end a case (BL-032:
    ``servicenow.resolved_states`` for an instance with custom states).
    """
    signals: list[LifecycleSignal] = []
    current = status
    for change in sorted(changes, key=lambda c: (c.ts, c.checkpoint or 0)):
        event = _event_for(
            change,
            current,
            integration_user=integration_user,
            integration_ids=integration_ids,
            automation_actors=automation_actors,
            resolved_states=resolved_states,
        )
        if event is None:
            continue
        after = TRANSITIONS.get((current, event))
        if after is None:
            continue  # not legal from here (e.g. a second takeover): idempotent replay
        signals.append(LifecycleSignal(event, change, current, after))
        current = after
    return signals


def _event_for(  # noqa: PLR0913 — one change plus the actor and state inputs
    change: ChangeRecord,
    current: CaseStatus,
    *,
    integration_user: str,
    integration_ids: Collection[str],
    automation_actors: Collection[str],
    resolved_states: Collection[str],
) -> CaseEvent | None:
    if change.source != "audit" or change.fieldname not in _TAKEOVER_FIELDS:
        return None
    new = change.newvalue or ""
    if change.fieldname == "state":
        if new in resolved_states:
            return CaseEvent.RESOLVED
        if current is CaseStatus.RESOLVED:
            return CaseEvent.REOPENED
    human = is_human_actor(
        change.user, integration_user=integration_user, automation_actors=automation_actors
    )
    if not human:
        return None
    if change.fieldname == "assigned_to" and new in integration_ids:
        return CaseEvent.HAND_BACK if current is CaseStatus.HUMAN_OWNED else None
    return CaseEvent.HUMAN_TAKEOVER


__all__ = ["RESOLVED_STATES", "LifecycleSignal", "is_human_actor", "lifecycle_signals"]
