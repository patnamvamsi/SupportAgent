"""Writing component summaries during onboarding (ADR-0023 §3, WP-222).

After the graph is built, each component (a service, job or UI) gets a short
summary from its facts and the code lines they cite, through the
``summarize_component`` slot, grounded by ``domain.component_summary``.

- **Cached by content hash.** A component whose brief is unchanged keeps its
  stored summary and inferred facts, with no model call.
- **Most connected first.** Services before jobs, the most connected first, so
  when the token budget runs out it is the least connected components that
  wait for the next run.
- Inferred facts join the build, so the graph saved for review carries them,
  proposed, with their quotes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ConfigDict, Field

from supportagent.app.graph import GraphBuild
from supportagent.app.readiness_store import TenantDocument
from supportagent.domain.component_summary import (
    Inferred,
    Sentence,
    Summary,
    brief,
    code_text,
    facts_text,
    ground,
)
from supportagent.domain.graph import Fact, build
from supportagent.domain.graph_prose import known_entities_text
from supportagent.domain.model.base import DomainModel

_COMPONENT_KINDS = ("Service", "Job", "UI")


class SentenceAnswer(DomainModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    text: str
    cites: list[str | int] = Field(default_factory=list)


class InferredAnswer(DomainModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    kind: str
    dst: str
    quote: str = ""


class SummaryAnswer(DomainModel):
    """``summarize_component``'s JSON; grounded before anything is kept."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)
    summary: list[SentenceAnswer] = Field(default_factory=list)
    inferred: list[InferredAnswer] = Field(default_factory=list)


#: ``summarize(component, facts, code, known_entities) -> answer or None``.
Summarizer = Callable[[str, str, str, str], SummaryAnswer | None]


class ComponentSummaryStore(TenantDocument):
    """Each component's summary, keyed by component id, with the digest it was written for."""

    table, column = "component_summaries", "summaries_json"


@dataclass(slots=True)
class SummaryRun:
    summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    written: int = 0
    reused: int = 0
    #: Components the model gave no answer for (unavailable, or the budget is spent).
    unwritten: list[str] = field(default_factory=list)
    inferred: int = 0


def summarize_components(
    built: GraphBuild,
    files: Mapping[str, str],
    summarize: Summarizer | None,
    previous: Mapping[str, Any] | None,
    *,
    version: str = "",
) -> tuple[GraphBuild, SummaryRun]:
    """Summaries for ``built``'s components, and the build with their inferred facts.

    ``version`` names the model and prompt: a summary another one wrote is written again.
    """
    graph = built.graph
    edges = list(graph.edges.values())
    stored: Mapping[str, Any] = (previous or {}).get("components", {})
    known = sorted((n.id, n.kind) for n in graph.nodes.values())
    listed = known_entities_text(known)
    ids = {node_id for node_id, _ in known}
    degree: dict[str, int] = {}
    for e in edges:
        for end in (e.src, e.dst):
            degree[end] = degree.get(end, 0) + 1
    components = sorted(
        (n for n in graph.nodes.values() if n.kind in _COMPONENT_KINDS),
        key=lambda n: (_COMPONENT_KINDS.index(n.kind), -degree.get(n.id, 0), n.id),
    )
    run = SummaryRun()
    added: list[Fact] = []
    for node in components:
        b = brief(node, edges, files)
        if not b.facts:
            continue
        old = stored.get(node.id)
        if (
            isinstance(old, Mapping)
            and old.get("digest") == b.digest
            and old.get("version", "") == version
        ):
            summary = _from_stored(node.id, old)
            run.reused += 1
        else:
            answer = (
                summarize(b.component, facts_text(b), code_text(b), listed) if summarize else None
            )
            if answer is None:
                run.unwritten.append(node.id)
                if isinstance(old, Mapping):  # an older summary is better than none
                    run.summaries[node.id] = dict(old) | {"stale": True}
                continue
            summary = ground(
                node.id,
                b,
                [(s.text, s.cites) for s in answer.summary],
                [(i.kind, i.dst, i.quote) for i in answer.inferred],
                ids,
            )
            run.written += 1
        kept = tuple(i for i in summary.inferred if i.dst in ids)
        added.extend(i.fact(node.id) for i in kept)
        run.inferred += len(kept)
        run.summaries[node.id] = _stored(summary) | {"version": version}
    if not added:
        return built, run
    facts = [*built.facts, *added]
    return (
        GraphBuild(
            graph=build(facts),
            facts=facts,
            rejected=built.rejected,
            pages_read=built.pages_read,
            pages_skipped=built.pages_skipped,
            failed=built.failed,
        ),
        run,
    )


def _stored(summary: Summary) -> dict[str, Any]:
    return {
        "digest": summary.digest,
        "sentences": [{"text": s.text, "cites": list(s.cites)} for s in summary.sentences],
        "facts": {str(n): text for n, text in summary.facts.items()},
        "inferred": [
            {"kind": i.kind, "dst": i.dst, "quote": i.quote, "source": i.source}
            for i in summary.inferred
        ],
        "dropped": summary.dropped,
    }


def _from_stored(component: str, raw: Mapping[str, Any]) -> Summary:
    return Summary(
        component=component,
        digest=str(raw.get("digest", "")),
        sentences=tuple(
            Sentence(str(s["text"]), tuple(int(c) for c in s.get("cites", [])))
            for s in raw.get("sentences", [])
        ),
        facts={int(n): str(t) for n, t in raw.get("facts", {}).items()},
        inferred=tuple(
            Inferred(str(i["kind"]), str(i["dst"]), str(i["quote"]), str(i["source"]))
            for i in raw.get("inferred", [])
        ),
        dropped=int(raw.get("dropped", 0)),
    )


__all__ = [
    "ComponentSummaryStore",
    "Summarizer",
    "SummaryAnswer",
    "SummaryRun",
    "summarize_components",
]
