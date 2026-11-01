"""Live evidence capture (ADR-0018, WP-E1): what to read for a past incident, and what came back.

Readiness replays each past incident from the evidence of its time. For a
real system that evidence comes from the agent's own read-only tools, asked
**as of** the incident: the same probes its runbooks would run. These say
which reads to make and sort what came back, honestly:

- **captured**: a result, or the tool's error (a refused read proves nothing,
  so it stays an error in the world);
- **not replayable**: a point-in-time tool (current locks, pods) can't see the
  past, so it isn't asked;
- **aged out**: the incident is older than the store keeps, or the store
  said so; reported, not read as "nothing was wrong";
- **unavailable**: no tool serves the capability here.

When any of the cause's own reads didn't come back, the world is left empty
and readiness doesn't replay the incident: without the decisive evidence the
replay would drift to another hypothesis (a lock seen only as the pool
exhaustion it caused), which live triage, reading the present, wouldn't.
Pure: the caller makes the reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from supportagent.domain.errors import HistoryUnavailable, ToolError
from supportagent.domain.model.runbook import Probe, Runbook

#: Why an incident in a class with a cause has no world.
Unreplayed = Literal["live_only", "aged_out", "unavailable", "failed"]


@dataclass(frozen=True, slots=True)
class PlannedRead:
    capability: str
    args: Mapping[str, str | int]
    #: One of the cause's own probes: without one, the incident isn't replayed.
    cause: bool


def applies(runbook: Runbook, label: str) -> bool:
    """A runbook's ``applies_when`` prefix-matches the label, as RCA selects it."""
    want = runbook.applies_when.classification
    return label == want or label.startswith(want + "/")


def capture_plan(
    label: str | None,
    cause_hypothesis: str | None,
    runbooks: Iterable[Runbook],
    probe: Probe | None,
) -> list[PlannedRead]:
    """The reads RCA would make for a case of ``label``: its runbooks' steps, then the
    cause's probe. One read per capability (a world holds one result each), with the
    first step's arguments."""
    steps: list[tuple[str, Mapping[str, str | int], bool]] = []
    for runbook in runbooks:
        if label is None or not applies(runbook, label):
            continue
        steps.extend((s.tool, s.args, s.hypothesis == cause_hypothesis) for s in runbook.steps)
    if probe is not None:
        steps.append((probe.tool, probe.args, True))
    planned: dict[str, PlannedRead] = {}
    for capability, args, cause in steps:
        first = planned.get(capability)
        if first is None:
            planned[capability] = PlannedRead(capability, dict(args), cause)
        elif cause and not first.cause:
            planned[capability] = PlannedRead(capability, first.args, True)
    return list(planned.values())


@dataclass
class CapturedEvidence:
    incident: str
    at: datetime
    world: dict[str, dict[str, object]] = field(default_factory=dict)
    not_replayable: list[str] = field(default_factory=list)
    aged_out: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    #: Set when the world was left empty: why the incident can't be replayed.
    unreplayed: Unreplayed | None = None

    def as_document(self) -> dict[str, object]:
        doc: dict[str, object] = {
            "incident": self.incident,
            "captured": "live",
            "captured_at": self.at.strftime("%Y-%m-%d %H:%M:%S"),
            "world": self.world,
        }
        for key in ("not_replayable", "aged_out", "unavailable"):
            if values := getattr(self, key):
                doc[key] = sorted(values)
        if self.unreplayed is not None:
            doc["unreplayed"] = self.unreplayed
        return doc


def capture(  # noqa: PLR0913 — the incident, its reads and how to make them
    incident: str,
    at: datetime,
    plan: Iterable[PlannedRead],
    *,
    now: datetime,
    retention_days: Mapping[str, int],
    available: Callable[[str], bool],
    replayable: Callable[[str], bool],
    read: Callable[[PlannedRead], Mapping[str, object]],
) -> CapturedEvidence:
    """Read each planned capability as of ``at`` and sort the answers (see the module)."""
    out = CapturedEvidence(incident, at)
    plan = list(plan)
    cause_reasons: list[Unreplayed] = []
    for item in plan:
        reason: Unreplayed | None = None
        if not available(item.capability):
            out.unavailable.append(item.capability)
            reason = "unavailable"
        elif not replayable(item.capability):
            out.not_replayable.append(item.capability)
            reason = "live_only"
        elif (days := retention_days.get(item.capability)) is not None and now - at > timedelta(
            days=days
        ):
            out.aged_out.append(item.capability)
            reason = "aged_out"
        else:
            try:
                out.world[item.capability] = {"result": dict(read(item))}
            except HistoryUnavailable:
                out.aged_out.append(item.capability)
                reason = "aged_out"
            except ToolError as exc:
                out.world[item.capability] = {"error": str(exc)[:300]}
                reason = "failed"
        if item.cause and reason is not None:
            cause_reasons.append(reason)
    # Without a cause read in the plan there is nothing to withhold: readiness replays
    # what came back and judges it (the cause's knowledge is the testable check's job).
    # With one missing, the replay can't be fair to the cause.
    if cause_reasons:
        out.world = {}
        # The most telling reason: the store has the data but can't serve the past,
        # then it's gone, then nothing serves it, then a read failed.
        order: tuple[Unreplayed, ...] = ("live_only", "aged_out", "unavailable", "failed")
        out.unreplayed = next(r for r in order if r in cause_reasons)
    return out


__all__ = [
    "CapturedEvidence",
    "PlannedRead",
    "Unreplayed",
    "applies",
    "capture",
    "capture_plan",
]
