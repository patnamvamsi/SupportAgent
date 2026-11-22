"""The onboarding autopilot's state (ADR-0017, WP-A1): steps, their inputs, and what's needed.

``supportagent onboard run`` takes a system through every onboarding step in
order. Each step records its **state** and the **digest** of its inputs:

- ``done``: it ran on these inputs; a re-run skips it while they are unchanged;
- ``blocked``: it can't finish until someone acts (``on``: the user, access, a plugin);
- ``failed``: it broke (a source unreachable); a re-run retries it.

A re-run is how the loop resumes: a plugin added, a request approved or a
source reachable again changes a step's inputs or clears its block.

**Needs** are what the system is waiting on a person for, ranked by the share
of incident weight each holds back: access to grant, plugins to provide,
facts to review. They are recomputed on every run from the current state,
so acting on one clears it the next time.

Pure: the caller runs the steps and supplies the digests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

Step = Literal[
    "ingest",
    "sense",
    "graph",
    "detectors",
    "incidents",
    "requests",
    "evidence",
    "readiness",
    "gaps",
]
STEPS: tuple[Step, ...] = (
    "ingest",
    "sense",
    "graph",
    "detectors",
    "incidents",
    "requests",
    "evidence",
    "readiness",
    "gaps",
)
State = Literal["done", "blocked", "failed"]
NeedKind = Literal["access", "plugin", "review", "evidence", "source", "shadow", "config"]
#: Who or what a need waits on, as shown.
NEED_LABELS: Mapping[str, str] = {
    "access": "Grant access",
    "plugin": "Provide a plugin",
    "review": "Review what onboarding found",
    "evidence": "Capture evidence",
    "source": "Fix a source",
    "shadow": "Run in shadow",
    "config": "Complete the configuration",
}


@dataclass(frozen=True, slots=True)
class StepRecord:
    step: Step
    state: State
    digest: str
    detail: str
    at: str
    #: For ``blocked``: what it waits on (``user``, ``access``, ``plugin``, ``config``).
    on: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "state": self.state,
            "digest": self.digest,
            "detail": self.detail,
            "at": self.at,
            "on": self.on,
        }

    @classmethod
    def of(cls, raw: Mapping[str, object]) -> StepRecord | None:
        step, state = raw.get("step"), raw.get("state")
        if step not in STEPS or state not in ("done", "blocked", "failed"):
            return None
        on = raw.get("on")
        return cls(
            step,
            state,
            str(raw.get("digest", "")),
            str(raw.get("detail", "")),
            str(raw.get("at", "")),
            str(on) if on is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Need:
    """One thing the system waits on a person for."""

    kind: NeedKind
    what: str
    #: The share of incident weight it holds back (0 when it holds back no class).
    weight: float
    how: str

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "what": self.what, "weight": self.weight, "how": self.how}


@dataclass
class OnboardingStatus:
    system: str
    steps: list[StepRecord] = field(default_factory=list)
    needs: list[Need] = field(default_factory=list)
    ready_share: float | None = None
    bar: float | None = None

    @property
    def ready(self) -> bool:
        return (
            self.ready_share is not None and self.bar is not None and self.ready_share >= self.bar
        )

    def ranked(self) -> list[Need]:
        return sorted(self.needs, key=lambda n: (-n.weight, n.kind, n.what))

    def as_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "system": self.system,
            "steps": [s.as_dict() for s in self.steps],
            "needs": [n.as_dict() for n in self.ranked()],
            "ready_share": self.ready_share,
            "bar": self.bar,
            "ready": self.ready,
        }

    def lines(self) -> list[str]:
        out = [f"{self.system}: onboarding"]
        for s in self.steps:
            on = f" (on {s.on})" if s.on else ""
            out.append(f"  {s.step:<10} {s.state}{on}: {s.detail}")
        if self.ready_share is not None and self.bar is not None:
            verdict = "ready to support" if self.ready else "not ready to support yet"
            out.append(
                f"  {verdict}: proven classes hold {self.ready_share:.0%} of the incident weight "
                f"(bar {self.bar:.0%})"
            )
        needs = self.ranked()
        out.append(f"  needed from you: {len(needs) or 'nothing'}")
        for n in needs:
            share = f"{n.weight:>4.0%}  " if n.weight else "      "
            out.append(f"    {share}{NEED_LABELS[n.kind]}: {n.what} -> {n.how}")
        return out


def digest(*parts: str | Iterable[tuple[str, str]]) -> str:
    """A stable digest of a step's inputs: strings, or ``(name, content digest)`` pairs."""
    h = hashlib.sha256()
    for part in parts:
        items = [("", part)] if isinstance(part, str) else sorted(part)
        for name, value in items:
            h.update(name.encode())
            h.update(b"\x00")
            h.update(value.encode())
            h.update(b"\x01")
    return h.hexdigest()[:24]


def should_run(previous: StepRecord | None, inputs: str) -> bool:
    """Run unless the step is ``done`` on exactly these inputs."""
    return previous is None or previous.state != "done" or previous.digest != inputs


__all__ = [
    "NEED_LABELS",
    "STEPS",
    "Need",
    "NeedKind",
    "OnboardingStatus",
    "State",
    "Step",
    "StepRecord",
    "digest",
    "should_run",
]
