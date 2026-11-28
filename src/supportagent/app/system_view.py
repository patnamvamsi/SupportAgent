"""The system as SupportAgent understands it (ADR-0023 §3, WP-225): components and their facts.

Composed from the graph under review (``GraphReview.facts``), nothing stored of
its own. A **component** is something that runs (a service, job or UI). For
each, what it does is grouped by meaning, each fact with the components on
the other side of it and the citations it came from:

========== ===================================================================
group      edges
========== ===================================================================
data flow  ``publishes`` / ``consumes`` a topic
storage    ``writes`` / ``reads`` / ``uses`` a table, schema or database
work       jobs ``part_of`` it, and what it is ``scheduled_by``
interfaces ``calls`` another component or a third party
runtime    ``depends_on``, ``runs_on``
========== ===================================================================

Rejected facts are left out; proposed ones are shown as such.

**Running or only defined** (WP-221 part 2): given the components the
deployment sees running (``observed``, measured from its container metrics),
a service is ``running`` or ``defined, not running``. A file can define a
service nobody runs any more; without container metrics nothing is said.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from supportagent.app.graph_review import FactView

#: What runs: the kinds a component page is about.
COMPONENT_PREFIXES = ("service:", "job:", "ui:")
#: Each group's edges, as (edge kind, the component is the edge's source).
GROUPS: Mapping[str, tuple[tuple[str, bool], ...]] = {
    "data flow": (("publishes", True), ("consumes", True)),
    "storage": (("writes", True), ("reads", True), ("uses", True)),
    "work": (("part_of", False), ("scheduled_by", True)),
    "interfaces": (("calls", True), ("calls", False)),
    "runtime": (("depends_on", True), ("depends_on", False), ("runs_on", True)),
}
_VERB = {
    ("publishes", True): "publishes",
    ("consumes", True): "consumes",
    ("writes", True): "writes",
    ("reads", True): "reads",
    ("uses", True): "uses",
    ("part_of", False): "runs job",
    ("scheduled_by", True): "scheduled by",
    ("calls", True): "calls",
    ("calls", False): "called by",
    ("depends_on", True): "depends on",
    ("depends_on", False): "needed by",
    ("runs_on", True): "runs on",
}
#: The edge kinds that share a thing between components (who else touches it).
_SHARED = {"publishes", "consumes", "writes", "reads", "uses"}


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    url: str | None = None
    quote: str | None = None


@dataclass(frozen=True, slots=True)
class Relation:
    """One fact about a component: ``verb`` ``target``, and who else touches the target."""

    verb: str
    target: str
    status: str
    citations: tuple[Citation, ...]
    others: tuple[tuple[str, str], ...] = ()  # (component, its verb)

    @property
    def target_name(self) -> str:
        return display(self.target)


@dataclass(slots=True)
class Component:
    id: str
    kind: str
    tech: tuple[str, ...] = ()
    status: str = "proposed"
    groups: dict[str, list[Relation]] = field(default_factory=dict)
    #: Seen running (``True``), defined but not running (``False``), or not measured.
    running: bool | None = None

    @property
    def name(self) -> str:
        return display(self.id)

    @property
    def service(self) -> str:
        """The name cases and the catalog use (``service:market-data`` → ``market-data``)."""
        return self.id.partition(":")[2]

    def count(self, group: str) -> int:
        return len(self.groups.get(group, []))


@dataclass(frozen=True, slots=True)
class SystemOverview:
    components: tuple[Component, ...]
    #: node kind → how many
    kinds: Mapping[str, int]
    facts: int
    proposed: int


def display(node_id: str) -> str:
    return node_id.partition(":")[2] or node_id


def _citations(fact: FactView) -> tuple[Citation, ...]:
    return tuple(
        Citation(str(s.get("ref") or ""), s.get("url"), s.get("quote"))
        for s in fact.sources
        if s.get("ref")
    )


def _live(facts: Iterable[FactView]) -> list[FactView]:
    return [f for f in facts if f.status != "rejected" and f.present]


def _same(name: str) -> str:
    return name.replace("_", "-").lower()


def components(
    facts: Sequence[FactView], observed: Collection[str] | None = None
) -> dict[str, Component]:
    """Every component, with its facts grouped (rejected and retired facts left out)."""
    kept = _live(facts)
    nodes = {f.src: f for f in kept if f.fact == "node"}
    edges = [f for f in kept if f.fact == "edge" and f.dst is not None]
    found: dict[str, Component] = {}
    seen = {_same(o) for o in observed} if observed is not None else None
    for node_id, node in sorted(nodes.items()):
        if node_id.startswith(COMPONENT_PREFIXES):
            running = (
                _same(node_id.partition(":")[2]) in seen
                if seen is not None and node.kind == "Service"
                else None
            )
            found[node_id] = Component(node_id, node.kind, node.tech, node.status, running=running)
    touching: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        if edge.kind in _SHARED and edge.src in found and edge.dst is not None:
            touching.setdefault(edge.dst, []).append((edge.src, edge.kind))
    for edge in edges:
        dst = edge.dst or ""
        for group, members in GROUPS.items():
            for kind, outgoing in members:
                if edge.kind != kind:
                    continue
                me, target = (edge.src, dst) if outgoing else (dst, edge.src)
                if me not in found:
                    continue
                others = tuple(
                    sorted(
                        (other, _VERB[(verb, True)])
                        for other, verb in touching.get(target, [])
                        if other != me
                    )
                )
                found[me].groups.setdefault(group, []).append(
                    Relation(
                        _VERB[(kind, outgoing)],
                        target,
                        edge.status,
                        _citations(edge),
                        others if edge.kind in _SHARED else (),
                    )
                )
    for component in found.values():
        for relations in component.groups.values():
            relations.sort(key=lambda r: (r.verb, r.target))
    return found


def overview(facts: Sequence[FactView], observed: Collection[str] | None = None) -> SystemOverview:
    kept = _live(facts)
    kinds: dict[str, int] = {}
    for f in kept:
        if f.fact == "node":
            kinds[f.kind] = kinds.get(f.kind, 0) + 1
    found = components(facts, observed)
    ranked = sorted(  # what runs first; services defined but not running last of them
        found.values(),
        key=lambda c: (
            c.kind != "Service",
            c.running is False,
            -sum(c.count(g) for g in GROUPS),
            c.id,
        ),
    )
    return SystemOverview(
        components=tuple(ranked),
        kinds=dict(sorted(kinds.items())),
        facts=len(kept),
        proposed=sum(1 for f in kept if f.status == "proposed"),
    )


def neighbourhood(facts: Sequence[FactView], component: str) -> list[FactView]:
    """The facts to draw around ``component``: its edges, and the other side's shared edges."""
    kept = _live(facts)
    own = [f for f in kept if f.fact == "edge" and component in (f.src, f.dst)]
    shared = {f.dst for f in own if f.kind in _SHARED and f.src == component}
    around = [
        f
        for f in kept
        if f.fact == "edge" and f.dst in shared and f.kind in _SHARED and f.src != component
    ]
    ends = {component} | {e for f in own + around for e in (f.src, f.dst) if e}
    nodes = [f for f in kept if f.fact == "node" and f.src in ends]
    return nodes + own + around


def graph_data(
    facts: Sequence[FactView], observed: Collection[str] | None = None
) -> dict[str, list[dict[str, object]]]:
    """The whole live graph for the explorer: every node (components marked) and edge.

    An edge whose end has no node fact still draws: that end gets a node of the
    kind its id names (``topic:x`` → ``topic``), marked ``proposed`` like the edge.
    """
    kept = _live(facts)
    found = components(facts, observed)
    nodes: dict[str, dict[str, object]] = {}
    for f in kept:
        if f.fact != "node":
            continue
        c = found.get(f.src)
        nodes[f.src] = {
            "id": f.src,
            "name": display(f.src),
            "kind": f.kind,
            "tech": list(f.tech),
            "status": f.status,
            "component": c is not None,
            "running": c.running if c is not None else None,
            "groups": {g: c.count(g) for g in GROUPS if c.count(g)} if c is not None else {},
            "sources": [str(s.get("ref")) for s in f.sources if s.get("ref")][:5],
        }
    edges: list[dict[str, object]] = []
    for f in kept:
        if f.fact != "edge" or f.dst is None:
            continue
        for end in (f.src, f.dst):
            nodes.setdefault(
                end,
                {
                    "id": end,
                    "name": display(end),
                    "kind": end.partition(":")[0] if ":" in end else "unknown",
                    "tech": [],
                    "status": "proposed",
                    "component": False,
                    "running": None,
                    "groups": {},
                    "sources": [],
                },
            )
        edges.append({"src": f.src, "dst": f.dst, "kind": f.kind, "status": f.status})
    return {"nodes": list(nodes.values()), "edges": edges}


__all__ = [
    "COMPONENT_PREFIXES",
    "GROUPS",
    "Citation",
    "Component",
    "Relation",
    "SystemOverview",
    "components",
    "display",
    "graph_data",
    "neighbourhood",
    "overview",
]
