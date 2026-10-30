"""Building a system's graph (ADR-0015 §3, WP-O3): layers 1-2 from structured sources, 3 from prose.

The structured facts come from the caller (the extractors over a workspace's
manifests, CMDB and live discovery). This service adds the prose layer: each
current documentation page goes through the ``extract_graph`` slot with the
entities found so far, and each fact in the answer is grounded
(``domain.graph_prose.ground``) before it joins. Then ``domain.graph.build``
merges everything, raising conflicts as questions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import ConfigDict, Field

from supportagent.domain.graph import EDGE_KINDS, Fact, SystemGraph, build
from supportagent.domain.graph_prose import (
    GraphExtraction,
    ProseEntity,
    ProseFact,
    Rejected,
    chunks,
    ground,
    known_entities_text,
)
from supportagent.domain.model.base import DomainModel
from supportagent.domain.plugins import DocPage


class EntityAnswer(DomainModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    id: str
    kind: str
    quote: str = ""


class FactAnswer(DomainModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    src: str
    kind: str
    dst: str
    quote: str = ""


class ExtractionAnswer(DomainModel):
    """``extract_graph``'s JSON. Unknown keys are ignored; the facts are grounded after."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    entities: list[EntityAnswer] = Field(default_factory=list)
    facts: list[FactAnswer] = Field(default_factory=list)

    def parsed(self) -> GraphExtraction:
        return GraphExtraction(
            entities=tuple(ProseEntity(e.id, e.kind, e.quote) for e in self.entities),
            facts=tuple(ProseFact(f.src, f.kind, f.dst, f.quote) for f in self.facts),
        )


#: ``extract(page_title, page_text, known_entities_text) -> answer or None``.
Extractor = Callable[[str, str, str], ExtractionAnswer | None]


@dataclass(slots=True)
class GraphBuild:
    graph: SystemGraph
    facts: list[Fact] = field(default_factory=list)
    rejected: list[tuple[str, Rejected]] = field(default_factory=list)
    #: Current pages read, and pages left out (legacy, draft, superseded).
    pages_read: list[str] = field(default_factory=list)
    pages_skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def build_graph(
    structured: Sequence[Fact], docs: Sequence[DocPage], extract: Extractor | None
) -> GraphBuild:
    """The system graph: structured facts, plus each current page's grounded facts."""
    first = build(structured)
    known = sorted((n.id, n.kind) for n in first.nodes.values())
    result = GraphBuild(graph=first, facts=list(structured))
    if extract is not None:
        listed = known_entities_text(known)
        ids = {node_id for node_id, _ in known}
        for page in docs:
            if not page.current:
                result.pages_skipped.append(page.page_id)
                continue
            result.pages_read.append(page.page_id)
            for part in chunks(page.text):
                answer = extract(page.title, part, listed)
                if answer is None:
                    result.failed.append(page.page_id)
                    continue
                facts, rejected = ground(
                    answer.parsed(), page_id=page.page_id, page_text=part, known=ids
                )
                result.facts.extend(facts)
                result.rejected.extend((page.page_id, r) for r in rejected)
    result.graph = build(result.facts)
    return result


def vocabulary() -> str:
    return ", ".join(sorted(EDGE_KINDS))


__all__ = ["ExtractionAnswer", "Extractor", "GraphBuild", "build_graph", "vocabulary"]
