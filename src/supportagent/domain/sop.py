"""SOP-driven requests (ADR-0014 part 1): a reviewed procedure proposes, the gate decides.

An SOP is a knowledge doc whose front matter carries a ``sop:`` block. It
names the hypotheses it applies to (``when``) and its steps, each an
**existing internal action kind** with a short note. Nothing in the SOP is
executed, and its body text never becomes a payload:

- A step is proposed only when RCA's outcome is SUPPORTED for a hypothesis
  in ``when``. That support comes from tool checks; a doc never supplies it.
- The SOP must be fresh (a stale SOP proposes nothing) and in the case's
  scope (its space, its services, internal audience).
- Each proposal has the literal scope ``sop:<doc>@<hash>:<step>:<kind>``,
  where ``hash`` covers the SOP block itself, so no service-wide setup grant
  covers it and an edited SOP is a new scope. A step waits for a human
  approval unless an auto-mode grant on exactly this scope covers it, and
  only when that grant's conditions hold (ADR-0014 part 2, ``domain.auto``).
- Provenance (doc, version, content hash, step) is recorded on the proposal
  and in the RCA artifact.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from supportagent.domain.kinds import ActionKind

#: Kinds an SOP step may propose: internal notes with a fixed payload
#: contract, each with a revert declared in ``domain.auto.REVERTS``. Field
#: updates, reruns and customer-facing kinds would need a revert of their own.
SOP_KINDS: frozenset[ActionKind] = frozenset({ActionKind.WORK_NOTE, ActionKind.ENRICH_PAGE})
MAX_SOP_STEPS = 5
MAX_NOTE_CHARS = 240


@dataclass(frozen=True, slots=True)
class SopStep:
    id: str
    kind: ActionKind
    #: Reviewed, redacted at ingest; at most ``MAX_NOTE_CHARS``.
    note: str


@dataclass(frozen=True, slots=True)
class Sop:
    #: Taxonomy hypothesis ids whose SUPPORTED outcome makes the steps apply.
    when: tuple[str, ...]
    steps: tuple[SopStep, ...]

    @property
    def digest(self) -> str:
        """sha256 of the block (``when`` and every step), the first 12 hex digits."""
        body = {
            "when": list(self.when),
            "steps": [[s.id, s.kind.value, s.note] for s in self.steps],
        }
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class SopMatch:
    """One SOP step that applies to a case: what the proposal and the artifact record."""

    doc_id: str
    title: str
    version: str | None
    url: str | None
    #: ``Sop.digest``: what the scope pins.
    content_hash: str
    hypothesis: str
    step: SopStep

    @property
    def key(self) -> str:
        """``<doc>/<step>``: what a lab truth names."""
        return f"{self.doc_id}/{self.step.id}"

    def ref(self) -> dict[str, object]:
        return {
            "doc": self.doc_id,
            "title": self.title,
            "version": self.version,
            "url": self.url,
            "hash": self.content_hash,
            "hypothesis": self.hypothesis,
            "step": self.step.id,
            "kind": self.step.kind.value,
            "note": self.step.note,
        }


def sop_scope(match: SopMatch) -> str:
    """``sop:<doc>@<hash>:<step>:<kind>``: literal, and new whenever the SOP block changes."""
    return step_scope(match.doc_id, match.content_hash, match.step)


def step_scope(doc_id: str, digest: str, step: SopStep) -> str:
    """The literal scope of one step of the SOP block with ``digest``."""
    return f"sop:{doc_id}@{digest}:{step.id}:{step.kind.value}"


def unique_steps(matches: Iterable[SopMatch]) -> list[SopMatch]:
    """``matches`` without repeats of one doc's step, in order."""
    seen: set[str] = set()
    out: list[SopMatch] = []
    for match in matches:
        if match.key not in seen:
            seen.add(match.key)
            out.append(match)
    return out


__all__ = [
    "MAX_NOTE_CHARS",
    "MAX_SOP_STEPS",
    "SOP_KINDS",
    "Sop",
    "SopMatch",
    "SopStep",
    "sop_scope",
    "step_scope",
    "unique_steps",
]
