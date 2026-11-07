"""Judging a traced object against its flow (ADR-0021 §5). Pure.

Each stage was looked up for one id: ``found`` (with its time, and whether it
rejected the object on purpose), ``missing`` (the lookup ran and found
nothing) or ``unknown`` (it couldn't run, or the stage has a correlation gap).
:func:`judge` walks the stages in order and returns one verdict:

- ``complete``: a terminal stage was found (the last one, or an earlier
  alternative end);
- ``rejected``: a stage rejected the object as the flow allows;
- ``in_progress``: the object waits at a stage, still within its time;
- ``stuck``: the object waits at a stage past its time: the first deviation;
- ``not_found``: not even the first stage has the id;
- ``cannot_tell``: the stage that decides it couldn't be looked up. Never a
  guess: the verdict names the stage and why.

A ``missing`` or ``unknown`` stage followed by a found one was passed, so it
isn't where the object stopped; a missing one there is reported as a gap in
correlation (the stage doesn't log the id). Stages found later than their
``within_min`` are listed as ``late`` whatever the verdict, but not after a
stage that wasn't seen: its time is unknown, so the limit has nothing to
count from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from supportagent.domain.model.flow import Flow

StageState = Literal["found", "missing", "unknown"]
VerdictKind = Literal["complete", "rejected", "in_progress", "stuck", "not_found", "cannot_tell"]


@dataclass(frozen=True, slots=True)
class StageObservation:
    stage: str
    state: StageState
    at: datetime | None = None
    rejected: bool = False
    #: Why a stage is unknown (no lookup, tool error, PII analyser unavailable).
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    #: The stage the verdict is about (where it stopped, rejected or can't be told).
    stage: str | None
    #: The time the object reached the previous stage, when waiting.
    since: datetime | None = None
    late: tuple[str, ...] = ()
    #: Stages passed though their lookup found nothing: the id isn't there.
    uncorrelated: tuple[str, ...] = ()
    reason: str = ""

    @property
    def deviation(self) -> str | None:
        """The first stage that departs from the flow, if any (the lab scores this)."""
        if self.kind in ("stuck", "not_found", "cannot_tell", "rejected"):
            return self.stage
        return self.late[0] if self.late else None


def judge(flow: Flow, seen: Mapping[str, StageObservation], *, now: datetime) -> Verdict:  # noqa: PLR0911 — one return per verdict
    stages = flow.stages
    obs = [
        seen.get(s.id, StageObservation(s.id, "unknown", reason="not looked up")) for s in stages
    ]
    last_found = max((i for i, o in enumerate(obs) if o.state == "found"), default=-1)
    late: list[str] = []
    uncorrelated: list[str] = []
    prev_at: datetime | None = None
    #: The previous stage wasn't seen, so ``prev_at`` isn't its time: no lateness here.
    gap = False
    for i, (stage, o) in enumerate(zip(stages, obs, strict=True)):
        if o.state == "found":
            if o.rejected:
                return Verdict(
                    "rejected", stage.id, late=tuple(late), uncorrelated=tuple(uncorrelated)
                )
            if (
                stage.within_min is not None
                and not gap
                and prev_at is not None
                and o.at is not None
                and o.at - prev_at > timedelta(minutes=stage.within_min)
            ):
                late.append(stage.id)
            prev_at = o.at or prev_at
            gap = False
            if stage.terminal:
                break
            continue
        if i < last_found:
            if o.state == "missing":
                uncorrelated.append(stage.id)
            gap = True
            continue
        done = (tuple(late), tuple(uncorrelated))
        if o.state == "unknown":
            return Verdict("cannot_tell", stage.id, prev_at, *done, reason=o.reason)
        if i == 0:
            return Verdict("not_found", stage.id, None, *done)
        if stage.within_min is None or prev_at is None:
            return Verdict("in_progress", stage.id, prev_at, *done, reason="no time limit declared")
        if now - prev_at <= timedelta(minutes=stage.within_min):
            return Verdict("in_progress", stage.id, prev_at, *done)
        return Verdict("stuck", stage.id, prev_at, *done)
    return Verdict("complete", None, None, tuple(late), tuple(uncorrelated))


__all__ = ["StageObservation", "StageState", "Verdict", "VerdictKind", "judge"]
