"""A knowledge doc's front matter (ADR-0011 §4, WP-M08): provenance and scope.

The markdown source reads it from a ``---`` YAML block; lab scenarios write it
inline. Unknown keys are rejected so a typo can't silently widen visibility.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import Field, field_validator

from supportagent.domain.kinds import ActionKind
from supportagent.domain.knowledge import DocMeta
from supportagent.domain.model.base import DomainModel
from supportagent.domain.sop import MAX_NOTE_CHARS, MAX_SOP_STEPS, SOP_KINDS, Sop, SopStep

_ID = r"^[a-z][a-z0-9_]*$"
_HYPOTHESIS = re.compile(_ID)


class SopStepMatter(DomainModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    kind: ActionKind
    note: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)

    @field_validator("kind", mode="before")
    @classmethod
    def _internal_note_kind(cls, value: object) -> ActionKind:
        try:
            kind = ActionKind(str(value))
        except ValueError:
            raise ValueError(f"unknown action kind {value!r}") from None  # noqa: TRY003
        if kind not in SOP_KINDS:
            allowed = ", ".join(sorted(k.value for k in SOP_KINDS))
            raise ValueError(f"an SOP step may propose only {allowed} (ADR-0014)")  # noqa: TRY003
        return kind


class SopMatter(DomainModel):
    """ADR-0014: a procedure's hypotheses and its steps (existing internal kinds only)."""

    when: list[str] = Field(min_length=1, max_length=5)
    steps: list[SopStepMatter] = Field(min_length=1, max_length=MAX_SOP_STEPS)

    @field_validator("when")
    @classmethod
    def _ids(cls, when: list[str]) -> list[str]:
        bad = [h for h in when if not _HYPOTHESIS.match(h)]
        if bad:
            raise ValueError(f"not hypothesis ids: {bad}")  # noqa: TRY003
        return when

    @field_validator("steps")
    @classmethod
    def _unique(cls, steps: list[SopStepMatter]) -> list[SopStepMatter]:
        ids = [s.id for s in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("SOP step ids must be unique")  # noqa: TRY003
        return steps

    def sop(self, redact: Callable[[str], str]) -> Sop:
        return Sop(
            when=tuple(self.when),
            steps=tuple(SopStep(s.id, s.kind, redact(s.note)) for s in self.steps),
        )


class DocFrontMatter(DomainModel):
    title: str = Field(min_length=1, max_length=200)
    url: str | None = None
    owner: str | None = None
    version: str | None = None
    last_updated: date | None = None
    visibility: Literal["internal", "customer"] = "internal"
    services: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list, max_length=5)
    sop: SopMatter | None = None

    def meta(
        self,
        *,
        doc_id: str,
        space: str,
        version: str | None = None,
        redact: Callable[[str], str],
    ) -> DocMeta:
        return DocMeta(
            doc_id=doc_id,
            space=space,
            title=self.title,
            url=self.url,
            owner=self.owner,
            version=self.version or version,
            last_updated=self.last_updated,
            visibility=self.visibility,
            services=tuple(self.services),
            labels=tuple(self.labels),
            hypotheses=tuple(self.hypotheses),
            sop=self.sop.sop(redact) if self.sop is not None else None,
        )


__all__ = ["DocFrontMatter", "SopMatter", "SopStepMatter"]
