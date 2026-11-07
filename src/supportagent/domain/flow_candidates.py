"""Candidate flows and flow drafts (ADR-0021 §2). Pure.

**Candidates.** People ask about business objects by id: "shipment
SHP-100231 hasn't arrived". :func:`candidates` reads ticket texts for ids,
groups them by shape (``SHP-`` and six digits becomes ``SHP-\\d{6}``), and
ranks the shapes by how many tickets ask about them. For each shape it
reports the word people put before the id (the object's name), example ids,
and the services those tickets' CIs belong to. Shapes that aren't business
objects are left out: the ticket numbers themselves, and shapes seen with
fewer than ``min_ids`` distinct ids (an error code repeats one value).

**Drafts.** :func:`draft_flow` turns a candidate into an unreviewed flow: one
stage per service the tickets name, ordered by how data moves between them
in the system graph (``calls``; ``publishes`` to a topic another service
``consumes``), each looked up in the logs by the traced id. A person names
the stages, sets their time limits and marks the draft reviewed; until then
nothing traces with it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.domain.graph import SystemGraph

_ID = re.compile(r"\b([A-Z]{2,6})([-_/]?)(\d{4,12})\b")
_WORD = re.compile(r"([A-Za-z]{3,20})\s*[:#]?\s*$")
_STOP = frozenset(
    {"for", "the", "and", "with", "from", "our", "this", "that", "number", "ref", "reference", "id"}
)
#: Edge kinds that order two services directly (``src`` before ``dst``).
_FLOWS_TO = frozenset({"calls", "writes"})


@dataclass(frozen=True, slots=True)
class Ticket:
    number: str
    text: str
    ci: str = ""


@dataclass(frozen=True, slots=True)
class Candidate:
    #: The id shape, as a flow's ``id_pattern``.
    pattern: str
    #: What people call it (``shipment``), or ``object`` when no word recurs.
    noun: str
    tickets: int
    examples: tuple[str, ...]
    #: The graph services the asking tickets' CIs belong to, most named first.
    services: tuple[str, ...] = ()
    cis: tuple[str, ...] = field(default=(), repr=False)

    def as_dict(self) -> dict[str, object]:
        return {
            "pattern": self.pattern,
            "object": self.noun,
            "tickets": self.tickets,
            "examples": list(self.examples),
            "services": list(self.services),
        }


def candidates(
    tickets: Sequence[Ticket],
    graph: SystemGraph | None = None,
    *,
    min_ids: int = 3,
    limit: int = 5,
) -> list[Candidate]:
    ticket_shapes = {_shape(m) for t in tickets for m in _ID.finditer(t.number)}
    ids: dict[str, set[str]] = {}
    asked: dict[str, set[str]] = {}
    words: dict[str, Counter[str]] = {}
    cis: dict[str, Counter[str]] = {}
    for t in tickets:
        for m in _ID.finditer(t.text):
            shape = _shape(m)
            if shape in ticket_shapes:
                continue
            ids.setdefault(shape, set()).add(m.group(0))
            asked.setdefault(shape, set()).add(t.number)
            word = _WORD.search(t.text[: m.start()])
            if word and word.group(1).lower() not in _STOP:
                words.setdefault(shape, Counter())[word.group(1).lower()] += 1
            if t.ci:
                cis.setdefault(shape, Counter())[t.ci] += 1
    out: list[Candidate] = []
    for shape, found in ids.items():
        if len(found) < min_ids:
            continue
        named = words.get(shape, Counter()).most_common(1)
        ci_counts = cis.get(shape, Counter())
        out.append(
            Candidate(
                pattern=shape,
                noun=named[0][0] if named else "object",
                tickets=len(asked[shape]),
                examples=tuple(sorted(found)[:3]),
                services=tuple(_services(graph, ci_counts)) if graph else (),
                cis=tuple(c for c, _ in ci_counts.most_common()),
            )
        )
    out.sort(key=lambda c: (-c.tickets, c.pattern))
    return out[:limit]


def _shape(m: re.Match[str]) -> str:
    prefix, sep, digits = m.group(1), m.group(2), m.group(3)
    return f"{re.escape(prefix)}{re.escape(sep)}\\d{{{len(digits)}}}"


def _services(graph: SystemGraph, ci_counts: Counter[str]) -> list[str]:
    by_ci: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.kind not in ("Service", "Job"):
            continue
        for source in node.sources:
            if source.startswith("cmdb:"):
                by_ci[source.removeprefix("cmdb:")] = node.id
    ranked: Counter[str] = Counter()
    for ci, n in ci_counts.items():
        if ci in by_ci:
            ranked[by_ci[ci]] += n
    return [s for s, _ in ranked.most_common()]


def data_order(graph: SystemGraph, services: Iterable[str]) -> list[str]:
    """``services`` in the order data moves between them; ties by name."""
    chosen = sorted(set(services))
    before: dict[str, set[str]] = {s: set() for s in chosen}
    publishers: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    for edge in graph.edges.values():
        if edge.kind in _FLOWS_TO and edge.src in before and edge.dst in before:
            before[edge.dst].add(edge.src)
        elif edge.kind == "publishes":
            publishers.setdefault(edge.dst, set()).add(edge.src)
        elif edge.kind == "consumes":
            consumers.setdefault(edge.dst, set()).add(edge.src)
    for topic, pubs in publishers.items():
        for consumer in consumers.get(topic, set()):
            for pub in pubs:
                if pub in before and consumer in before and pub != consumer:
                    before[consumer].add(pub)
    order: list[str] = []
    remaining = dict(before)
    while remaining:
        ready = sorted(s for s, deps in remaining.items() if not (deps & remaining.keys()))
        if not ready:  # a cycle: the rest by name, for the reviewer to sort out
            ready = sorted(remaining)
        order.append(ready[0])
        del remaining[ready[0]]
    return order


def draft_flow(candidate: Candidate, graph: SystemGraph, *, flow_id: str) -> dict[str, object]:
    """An unreviewed flow for ``candidate`` (a mapping, written as YAML for review)."""
    services = data_order(graph, candidate.services)
    stages: list[Mapping[str, object]] = []
    for i, service in enumerate(services):
        name = service.split(":", 1)[-1]
        stage: dict[str, object] = {
            "id": _slug(name),
            "title": f"TODO: what {name} does to the {candidate.noun}",
            "component": service,
            "lookup": {
                "capability": "logs.search",
                "source": "logs",
                "args": {
                    "query": '{service="{{service}}"} |= "$object.id"',
                    "service": "$service.name",
                },
                "found": "matches[]",
                "at": "matches[].ts",
                "fields": ["matches[].ts", "matches[].line"],
            },
        }
        if i == len(services) - 1:
            stage["terminal"] = True
        stages.append(stage)
    return {
        "flow": flow_id,
        "title": f"TODO: the {candidate.noun} from start to finish",
        "object": candidate.noun,
        "id_pattern": candidate.pattern,
        "reviewed": False,
        "sources": [f"tickets: {candidate.tickets} asking about {candidate.pattern}"],
        "stages": stages,
    }


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (slug or "stage")[:40].ljust(2, "x")


__all__ = ["Candidate", "Ticket", "candidates", "data_order", "draft_flow"]
