"""A knowledge doc's front matter (ADR-0011 §4, WP-M08): provenance and scope.

The markdown source reads it from a ``---`` YAML block; lab scenarios write it
inline. Unknown keys are rejected so a typo can't silently widen visibility.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from supportagent.domain.knowledge import DocMeta
from supportagent.domain.model.base import DomainModel

_ID = r"^[a-z][a-z0-9_]*$"


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

    def meta(self, *, doc_id: str, space: str, version: str | None = None) -> DocMeta:
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
        )


__all__ = ["DocFrontMatter"]
