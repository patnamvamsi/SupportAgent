"""The gap plan (ADR-0015 §4, WP-O5): from the incident map to a ranked list of requests.

Every plugin the system needs declares least-privilege access contracts, and
each contract says where its targets are (``TargetFinder``). A **request** is
one contract on one target: its scope is the contract's grant, quoted
verbatim, never anything written per install, so it can't ask for more than
the plugin declared.

A request **unlocks** an incident class when the capabilities its grant
serves are ones the class's cause needs (the capabilities of the plugin that
declares the cause, plus that plugin's ``needs``) and, for a target in the
system graph, when the class's CIs reach the target in the graph (a service
that uses a schema of that database, a job that runs on that cluster).
Requests are ranked by the share of incident weight they unlock, then by
the P1s among it.

A class no request unlocks is a **gap**: an extension proposal for a person,
with its incidents attached as candidate scenarios.

Pure: the caller gives the detected plugins, the graph, the snapshot and the map.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from supportagent.domain.graph import SystemGraph
from supportagent.domain.incident_map import IncidentClass, IncidentMap
from supportagent.domain.model.plugin import AccessContract, PluginManifest, TargetFinder
from supportagent.domain.plugins import SourceSnapshot

#: What the built-in causes need to be tested (they belong to no plugin).
BUILT_IN_NEEDS: Mapping[str, frozenset[str]] = {
    "recent_change_push": frozenset({"changes.recent"}),
    "upstream_provider": frozenset({"logs.search", "metrics.query"}),
    "dependency_failure": frozenset({"logs.search", "metrics.query"}),
    "capacity_decay": frozenset({"metrics.query"}),
    "traffic_spike": frozenset({"metrics.query"}),
    "data_volume": frozenset({"metrics.query", "logs.search"}),
}
_REACH = 3


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    #: Where it was found: a graph node, a repository, a CMDB item, a page.
    found_in: str
    node: str | None = None


@dataclass(frozen=True, slots=True)
class AccessRequest:
    plugin: str
    access: str
    target: str
    found_in: str
    #: The contract's grant, verbatim: the request's whole scope.
    scope: str
    description: str
    verify_can: tuple[str, ...]
    verify_cannot: tuple[str, ...]
    capabilities: tuple[str, ...]
    #: The incident classes it unlocks, their share of the history's weight, and P1s.
    unlocks: tuple[str, ...]
    share: float
    p1: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.plugin, self.access, self.target)

    @property
    def why(self) -> str:
        if not self.unlocks:
            return "unlocks no past incident class on its own"
        p1 = f", including {self.p1} P1" + ("s" if self.p1 != 1 else "") if self.p1 else ""
        n = len(self.unlocks)
        return (
            f"unlocks {n} incident class{'es' if n != 1 else ''}, "
            f"{self.share:.0%} of the history's weight{p1}"
        )


@dataclass(frozen=True, slots=True)
class Gap:
    """An incident class nothing requested can test: an extension proposal."""

    class_id: str
    cause: str | None
    label: str | None
    share: float
    incidents: tuple[str, ...]
    why: str


@dataclass(frozen=True, slots=True)
class GapPlan:
    requests: tuple[AccessRequest, ...]
    gaps: tuple[Gap, ...]
    #: The share of incident weight the requests together unlock.
    unlocked: float


def target_name(node_id: str, tech: Collection[str] = ()) -> str:
    """``database:paydb`` -> ``paydb``; ``runtime:emr/emr-batch`` -> ``emr-batch``.

    ADR-0016: a first segment that names the node's own technology (``emr``,
    ``kafka``) is an area, not part of the name; ``runtime:prod-eu1/payments``
    (a Kubernetes namespace) keeps its cluster.
    """
    rest = node_id.split(":", 1)[-1]
    area, _, tail = rest.partition("/")
    return tail if tail and area in tech else rest


def find_targets(finder: TargetFinder, graph: SystemGraph, snap: SourceSnapshot) -> list[Target]:
    if finder.graph is not None:
        found = _in_graph(finder.graph, finder.tech, graph)
    elif finder.repos is not None:
        found = _in_repos(finder.repos, snap)
    elif finder.cmdb_class is not None:
        found = _in_cmdb(finder.cmdb_class, finder.name, snap)
    else:
        found = _in_docs(finder.docs or "", snap)
    first: dict[str, Target] = {}
    for target in found:
        first.setdefault(target.name, target)  # one request per target, its first sighting
    return list(first.values())


def _in_graph(kind: str, tech: str | None, graph: SystemGraph) -> list[Target]:
    return [
        Target(target_name(n.id, n.tech), f"graph node {n.id}", n.id)
        for n in sorted(graph.nodes.values(), key=lambda n: n.id)
        if n.kind == kind and (tech is None or tech in n.tech)
    ]


def _in_repos(host: str, snap: SourceSnapshot) -> list[Target]:
    found = []
    for repo in snap.repos:
        repo_host, _, path = repo.partition(":")
        if repo_host == host and path:
            found.append(Target(path, f"repository {repo}"))
    return found


def _in_cmdb(cls: str, name: str | None, snap: SourceSnapshot) -> list[Target]:
    pattern = re.compile(name) if name else None
    found = []
    for ci, ci_cls in snap.cis:
        if ci_cls != cls:
            continue
        if pattern is None:
            found.append(Target(ci, f"CMDB item {ci!r} ({ci_cls})"))
        elif (match := pattern.search(ci)) is not None:
            found.append(Target(match.group(1), f"CMDB item {ci!r} ({ci_cls})"))
    return found


def _in_docs(regex: str, snap: SourceSnapshot) -> list[Target]:
    pattern = re.compile(regex, re.IGNORECASE)
    return [
        Target(m.group(1).lower(), f"page {page.page_id} ({page.title})")
        for page in snap.docs
        if page.current
        for m in pattern.finditer(page.text)
    ]


def _served(plugin: PluginManifest, access: str) -> tuple[str, ...]:
    declared = list(plugin.provides.capabilities)
    out: list[str] = []
    for backend in plugin.provides.backends:
        if backend.access != access:
            continue
        pattern = backend.capability
        for name in declared:
            if name == pattern or (pattern.endswith(".*") and name.startswith(pattern[:-1])):
                out.append(name)
    return tuple(dict.fromkeys(out))


def _needs(cause: str | None, plugins: Sequence[PluginManifest]) -> frozenset[str]:
    if cause is None:
        return frozenset()
    if cause in BUILT_IN_NEEDS:
        return BUILT_IN_NEEDS[cause]
    for plugin in plugins:
        if any(c.id == cause for c in plugin.provides.causes):
            return frozenset(plugin.provides.capabilities) | frozenset(plugin.provides.needs)
    return frozenset()


def _reach(graph: SystemGraph, start: Iterable[str]) -> set[str]:
    out: dict[str, set[str]] = {}
    for src, _, dst in graph.edges:
        out.setdefault(src, set()).add(dst)
    seen = set(start)
    frontier = set(seen)
    for _ in range(_REACH):
        frontier = {d for n in frontier for d in out.get(n, ())} - seen
        seen |= frontier
    return seen


def _class_nodes(cls: IncidentClass, cis: Mapping[str, str], graph: SystemGraph) -> set[str]:
    names = {cis[n] for n in cls.incidents if n in cis}
    return {n.id for n in graph.nodes.values() if target_name(n.id, n.tech) in names}


def plan(
    plugins: Sequence[PluginManifest],
    graph: SystemGraph,
    snap: SourceSnapshot,
    incidents: IncidentMap,
    incident_cis: Mapping[str, str],
) -> GapPlan:
    """The ranked requests for the detected ``plugins``, and the gaps they leave.

    ``incident_cis`` maps an incident number to its CI's name.
    """
    reach = {
        c.id: _reach(graph, nodes)
        for c in incidents.classes
        if (nodes := _class_nodes(c, incident_cis, graph))
    }
    requests: list[AccessRequest] = []
    for plugin in plugins:
        for access, contract in sorted(plugin.requires.items()):
            served = _served(plugin, access)
            for target in find_targets(contract.find, graph, snap):
                unlocked = [
                    c
                    for c in incidents.classes
                    if set(served) & _needs(c.cause, plugins)
                    and (target.node is None or c.id not in reach or target.node in reach[c.id])
                ]
                requests.append(
                    _request(
                        plugin,
                        access=access,
                        contract=contract,
                        target=target,
                        served=served,
                        unlocked=unlocked,
                    )
                )
    requests.sort(key=lambda r: (-r.share, -r.p1, r.plugin, r.target))
    served_all = {cap for r in requests for cap in r.capabilities}
    unlocked_ids = {u for r in requests for u in r.unlocks}
    gaps = tuple(
        Gap(
            c.id,
            c.cause,
            c.label,
            c.share,
            c.incidents,
            "no plugin names a cause for it"
            if c.cause is None
            else "nothing requested serves what its cause needs: "
            + ", ".join(sorted(_needs(c.cause, plugins) - served_all) or ["(unknown)"]),
        )
        for c in incidents.classes
        if c.id not in unlocked_ids
    )
    unlocked_share = sum(c.share for c in incidents.classes if c.id in unlocked_ids)
    return GapPlan(tuple(requests), gaps, round(unlocked_share, 3))


def _request(  # noqa: PLR0913 — one request's parts
    plugin: PluginManifest,
    *,
    access: str,
    contract: AccessContract,
    target: Target,
    served: tuple[str, ...],
    unlocked: Sequence[IncidentClass],
) -> AccessRequest:
    return AccessRequest(
        plugin=plugin.id,
        access=access,
        target=target.name,
        found_in=target.found_in,
        scope=contract.grant,
        description=contract.description,
        verify_can=tuple(contract.verify_can),
        verify_cannot=tuple(contract.verify_cannot),
        capabilities=served,
        unlocks=tuple(c.id for c in unlocked),
        share=round(sum(c.share for c in unlocked), 3),
        p1=sum(c.severities.get("P1", 0) for c in unlocked),
    )


__all__ = [
    "BUILT_IN_NEEDS",
    "AccessRequest",
    "Gap",
    "GapPlan",
    "Target",
    "find_targets",
    "plan",
    "target_name",
]
