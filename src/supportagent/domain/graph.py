"""The system graph (ADR-0015 §3, WP-O3): what an instance understands about a system.

**Vocabulary** (ids are lower case):

========== ======================================================================
kind       id
========== ======================================================================
Service    ``service:<name>``: a long-running API or worker
Job        ``job:<name>``: batch, stream, scheduled task, connector
UI         ``ui:<name>``
Database   ``database:<name>``
Datastore  ``schema:<db>.<schema>`` or ``table:<db>.<table>``
Topic      ``topic:<name>``
Runtime    ``runtime:<cluster>/<namespace>`` (Kubernetes), ``runtime:emr/<cluster>``,
           ``runtime:snowflake/<warehouse>``, ``runtime:airflow/<env>``,
           ``runtime:kafka/<cluster>``
External   ``external:<org>``: a third party (named by its host's organisation)
========== ======================================================================

Edges: ``calls`` ``uses`` ``reads`` ``writes`` ``consumes`` ``publishes``
``runs_on`` ``scheduled_by`` ``part_of`` ``depends_on``.

**Facts** arrive in layers, in order of trust: ``1`` structured sources
(manifests, configuration, the CMDB), ``2`` live read-only discovery, ``3``
prose (documentation, with a verbatim quote). :func:`build` merges them:

- **aliases** (two names for one entity) are resolved first;
- a node or edge keeps every source that asserted it;
- **documentation relates, it doesn't discover**: a node only prose names
  is dropped (a person can still add it in review), so a page can't revive a
  retired component or invent one from a generic phrase;
- **the most trusted layer names a relation**: a prose edge between two
  nodes a structured or live edge already joins, under another kind, yields;
- a node's **id prefix names its kind** (a CMDB application CI reconciled to
  ``runtime:airflow/<env>`` is a Runtime);
- **config beats the CMDB** on what a component connects to: a CMDB
  ``uses database:X`` for a component whose configuration connects it to a
  different database is dropped and becomes a **question** for review;
- **subsumption**: an edge to a database or runtime is dropped when the
  same component has a finer edge to something ``part_of`` it;
- an edge whose endpoint no fact describes is dropped.

The graph is guidance for RCA, never evidence (ADR-0011 §4). Pure.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

NodeKind = Literal["Service", "Job", "UI", "Database", "Datastore", "Topic", "Runtime", "External"]
EDGE_KINDS = frozenset(
    {
        "calls",
        "uses",
        "reads",
        "writes",
        "consumes",
        "publishes",
        "runs_on",
        "scheduled_by",
        "part_of",
        "depends_on",
    }
)
_CONTAINERS = frozenset({"Database", "Runtime"})
#: An id's prefix names its kind (the vocabulary above); it beats any source's kind.
_PREFIX_KIND: dict[str, NodeKind] = {
    "service": "Service",
    "job": "Job",
    "ui": "UI",
    "database": "Database",
    "schema": "Datastore",
    "table": "Datastore",
    "topic": "Topic",
    "runtime": "Runtime",
    "external": "External",
}
#: Edge kinds that say what a component touches; a finer one subsumes a coarse one.
_TOUCHES = frozenset({"uses", "reads", "writes", "consumes", "publishes", "depends_on"})
Layer = Literal[1, 2, 3]
_PROSE = 3


@dataclass(frozen=True, slots=True)
class NodeFact:
    id: str
    kind: NodeKind
    source: str
    layer: Layer
    extractor: str
    tech: tuple[str, ...] = ()
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class EdgeFact:
    src: str
    kind: str
    dst: str
    source: str
    layer: Layer
    extractor: str
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class AliasFact:
    """``alias`` and ``canonical`` name one entity (a CMDB CI and its task name, say)."""

    alias: str
    canonical: str
    source: str
    layer: Layer
    extractor: str


Fact = NodeFact | EdgeFact | AliasFact


@dataclass(slots=True)
class Node:
    id: str
    kind: NodeKind
    tech: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    layer: int = 3


@dataclass(slots=True)
class Edge:
    src: str
    kind: str
    dst: str
    sources: set[str] = field(default_factory=set)
    layer: int = 3

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.kind, self.dst)


@dataclass(frozen=True, slots=True)
class QuestionOption:
    """One way to settle a question; ``edge`` is added to the graph when it is chosen."""

    label: str
    edge: tuple[str, str, str] | None = None


@dataclass(frozen=True, slots=True)
class Question:
    """A conflict between sources, raised for a person instead of resolved silently."""

    about: str
    sources: tuple[str, ...]
    detail: str
    options: tuple[QuestionOption, ...] = ()


@dataclass(slots=True)
class SystemGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)
    #: Facts dropped and why (unknown endpoint, subsumed, CMDB overruled).
    dropped: list[str] = field(default_factory=list)


def build(facts: Iterable[Fact]) -> SystemGraph:
    facts = list(facts)
    alias = _aliases(f for f in facts if isinstance(f, AliasFact))
    graph = SystemGraph()
    for fact in facts:
        if isinstance(fact, NodeFact):
            node_id = alias.get(fact.id, fact.id)
            node = graph.nodes.get(node_id)
            if node is None or fact.layer < node.layer:
                kind = fact.kind if node is None or fact.layer < node.layer else node.kind
                merged = Node(
                    node_id,
                    kind,
                    set(node.tech) if node else set(),
                    set(node.sources) if node else set(),
                    fact.layer,
                )
                graph.nodes[node_id] = node = merged
            node.tech |= set(fact.tech)
            node.sources.add(fact.source)
    for node in graph.nodes.values():
        node.kind = _PREFIX_KIND.get(node.id.split(":", 1)[0], node.kind)
    for node_id in [n.id for n in graph.nodes.values() if n.layer >= _PROSE]:
        del graph.nodes[node_id]
        graph.dropped.append(f"{node_id}: only the documentation names it")
    edges = [f for f in facts if isinstance(f, EdgeFact) and f.kind in EDGE_KINDS]
    trusted = {
        frozenset((alias.get(f.src, f.src), alias.get(f.dst, f.dst)))
        for f in edges
        if f.layer < _PROSE
    }
    for fact in sorted(edges, key=lambda f: f.layer):
        src, dst = alias.get(fact.src, fact.src), alias.get(fact.dst, fact.dst)
        if src == dst:
            continue
        if src not in graph.nodes or dst not in graph.nodes:
            graph.dropped.append(f"{src} {fact.kind} {dst}: no fact describes an endpoint")
            continue
        key = (src, fact.kind, dst)
        if fact.layer >= _PROSE and key not in graph.edges and frozenset((src, dst)) in trusted:
            graph.dropped.append(f"{src} {fact.kind} {dst}: a more trusted source names it")
            continue
        edge = graph.edges.setdefault(key, Edge(src, fact.kind, dst))
        edge.sources.add(fact.source)
        edge.layer = min(edge.layer, fact.layer)
    _overrule_cmdb(graph)
    _subsume(graph)
    return graph


def _aliases(facts: Iterable[AliasFact]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for fact in facts:
        mapping[fact.alias] = fact.canonical
    # Follow chains (a -> b -> c) to the end, without looping.
    for start in list(mapping):
        seen = {start}
        target = mapping[start]
        while target in mapping and target not in seen:
            seen.add(target)
            target = mapping[target]
        mapping[start] = target
    return mapping


def _database_of(graph: SystemGraph, node_id: str) -> str | None:
    node = graph.nodes.get(node_id)
    if node is None:
        return None
    if node.kind == "Database":
        return node_id
    for (src, kind, dst), _ in graph.edges.items():
        if src == node_id and kind == "part_of" and graph.nodes[dst].kind == "Database":
            return dst
    return None


def _overrule_cmdb(graph: SystemGraph) -> None:
    """A component's configuration says which database it uses; a stale CMDB edge loses."""
    for edge in list(graph.edges.values()):
        if edge.kind != "uses" or graph.nodes[edge.dst].kind != "Database":
            continue
        if not edge.sources or not all(s.startswith("cmdb:") for s in edge.sources):
            continue
        configured = {
            db
            for other in graph.edges.values()
            if other.src == edge.src
            and other.kind in ("uses", "reads", "writes")
            and any(s.startswith("git:") for s in other.sources)
            and (db := _database_of(graph, other.dst)) is not None
        }
        if configured and edge.dst not in configured:
            del graph.edges[edge.key]
            graph.questions.append(
                Question(
                    about=f"{edge.src} uses which database",
                    sources=tuple(sorted(edge.sources)),
                    detail=(
                        f"the CMDB says {edge.dst}; its configuration connects it to "
                        f"{', '.join(sorted(configured))}"
                    ),
                    options=(
                        QuestionOption(f"Keep the configuration: {', '.join(sorted(configured))}"),
                        QuestionOption(f"The CMDB is right: it uses {edge.dst}", edge.key),
                    ),
                )
            )
            graph.dropped.append(f"{edge.src} uses {edge.dst}: overruled by configuration")


def _subsume(graph: SystemGraph) -> None:
    parts: dict[str, set[str]] = {}
    for src, kind, dst in graph.edges:
        if kind == "part_of":
            parts.setdefault(dst, set()).add(src)
    for edge in list(graph.edges.values()):
        if edge.kind not in _TOUCHES or graph.nodes[edge.dst].kind not in _CONTAINERS:
            continue
        inside = parts.get(edge.dst, set())
        finer = any(
            other.src == edge.src and other.kind in _TOUCHES and other.dst in inside
            for other in graph.edges.values()
        )
        if finer:
            del graph.edges[edge.key]
            graph.dropped.append(f"{edge.src} {edge.kind} {edge.dst}: subsumed by a finer edge")


def node_ids(graph: SystemGraph) -> list[tuple[str, str]]:
    return sorted((n.id, n.kind) for n in graph.nodes.values())


def edge_keys(graph: SystemGraph) -> list[tuple[str, str, str]]:
    return sorted(graph.edges)


def slug(name: str) -> str:
    """An id segment: lower case, spaces and underscores to hyphens, nothing else changed."""
    return name.strip().lower().replace(" ", "-")


def facts_by_source(facts: Sequence[Fact]) -> Mapping[str, int]:
    counts: dict[str, int] = {}
    for fact in facts:
        kind = fact.source.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


__all__ = [
    "EDGE_KINDS",
    "AliasFact",
    "Edge",
    "EdgeFact",
    "Fact",
    "Node",
    "NodeFact",
    "NodeKind",
    "Question",
    "QuestionOption",
    "SystemGraph",
    "build",
    "edge_keys",
    "facts_by_source",
    "node_ids",
    "slug",
]
