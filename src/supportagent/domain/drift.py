"""Drift (ADR-0015 P7, WP-O8): what changed in a system since it was last onboarded.

Onboarding (P1 to P4) re-runs on the system as it is now, and its result is
compared with the last accepted **snapshot**: the plugins it needs, the
graph's nodes and edges, the incident classes and their shares, and the
access requests. The **re-onboarding diff** is a list of findings, each
saying what a person should do about it:

- a plugin newly needed (review it) or no longer needed;
- a node added, removed or **renamed**: a removed and an added node of the
  same kind whose relationships, with one renamed into the other, mostly
  agree (review the graph facts);
- an edge added or removed (not when it only follows a rename);
- an incident class that is new, gone, or that grew: its share by ``grew``
  or more, or its incidents by half (replay readiness); a new class whose cause no detected plugin
  declares is **uncovered** (an extension proposal);
- incidents with no known cause growing (the no-cause class);
- an access request newly asked for (decide it) or no longer needed.

Pure: the caller builds both snapshots.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

FindingKind = Literal[
    "plugin_added",
    "plugin_removed",
    "node_added",
    "node_removed",
    "node_renamed",
    "edge_added",
    "edge_removed",
    "class_new",
    "class_gone",
    "class_grew",
    "class_uncovered",
    "uncaused_grew",
    "request_new",
    "request_gone",
]
#: What re-onboarding asks of a person, per kind of finding.
ACTIONS: Mapping[str, str] = {
    "plugin_added": "review the plugin and its access requests",
    "plugin_removed": "confirm the stack no longer uses it",
    "node_added": "review the new facts at /ui/onboarding",
    "node_removed": "confirm it is gone at /ui/onboarding",
    "node_renamed": "confirm the rename at /ui/onboarding",
    "edge_added": "review the new facts at /ui/onboarding",
    "edge_removed": "confirm it is gone at /ui/onboarding",
    "class_new": "replay readiness (onboard readiness)",
    "class_gone": "replay readiness (onboard readiness)",
    "class_grew": "replay readiness (onboard readiness)",
    "class_uncovered": "no plugin covers it: an extension proposal",
    "uncaused_grew": "read the new incidents: a cause the catalogue lacks?",
    "request_new": "decide it at /ui/onboarding/requests",
    "request_gone": "revoke the access if it was granted",
}
_RENAME_MIN = 0.5
_TRIPLE = 3
#: A class whose incident count grows by half, and by at least this many, grew.
_GREW_MIN = 3


@dataclass(frozen=True, slots=True)
class ClassShape:
    cause: str | None
    label: str | None
    share: float
    incidents: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one onboarding run understood. JSON round-trips via :meth:`as_dict`."""

    plugins: frozenset[str] = frozenset()
    #: node id -> kind
    nodes: Mapping[str, str] = field(default_factory=dict)
    edges: frozenset[tuple[str, str, str]] = frozenset()
    #: class id -> its shape
    classes: Mapping[str, ClassShape] = field(default_factory=dict)
    #: causes the detected plugins (or the built-ins) declare
    known_causes: frozenset[str] = frozenset()
    #: ``plugin/access/target``
    requests: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "plugins": sorted(self.plugins),
            "nodes": dict(sorted(self.nodes.items())),
            "edges": [list(e) for e in sorted(self.edges)],
            "classes": {
                k: {"cause": c.cause, "label": c.label, "share": c.share, "incidents": c.incidents}
                for k, c in sorted(self.classes.items())
            },
            "known_causes": sorted(self.known_causes),
            "requests": sorted(self.requests),
        }

    @classmethod
    def of(cls, raw: Mapping[str, object]) -> Snapshot:
        def strings(key: str) -> list[str]:
            value = raw.get(key)
            return [str(x) for x in value] if isinstance(value, list) else []

        nodes = raw.get("nodes")
        edges = raw.get("edges")
        classes = raw.get("classes")
        return cls(
            plugins=frozenset(strings("plugins")),
            nodes={str(k): str(v) for k, v in nodes.items()} if isinstance(nodes, dict) else {},
            edges=frozenset(
                (str(e[0]), str(e[1]), str(e[2]))
                for e in (edges if isinstance(edges, list) else [])
                if isinstance(e, list) and len(e) == _TRIPLE
            ),
            classes={
                str(k): ClassShape(
                    cause=None if v.get("cause") is None else str(v["cause"]),
                    label=None if v.get("label") is None else str(v["label"]),
                    share=float(v.get("share", 0.0)),
                    incidents=int(v.get("incidents", 0)),
                )
                for k, v in (classes.items() if isinstance(classes, dict) else [])
                if isinstance(v, dict)
            },
            known_causes=frozenset(strings("known_causes")),
            requests=frozenset(strings("requests")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    subject: str
    detail: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.subject)

    @property
    def action(self) -> str:
        return ACTIONS[self.kind]


def diff(old: Snapshot, new: Snapshot, *, grew: float = 0.05) -> list[Finding]:
    """The re-onboarding diff, in a stable order: plugins, graph, incidents, requests."""
    out: list[Finding] = []
    out += [Finding("plugin_added", p, "now detected") for p in sorted(new.plugins - old.plugins)]
    out += [
        Finding("plugin_removed", p, "no longer detected")
        for p in sorted(old.plugins - new.plugins)
    ]
    out += _graph(old, new)
    out += _classes(old, new, grew)
    out += [Finding("request_new", r, "asked for") for r in sorted(new.requests - old.requests)]
    out += [
        Finding("request_gone", r, "no longer asked for")
        for r in sorted(old.requests - new.requests)
    ]
    return out


def _graph(old: Snapshot, new: Snapshot) -> list[Finding]:
    added = {n for n in new.nodes if n not in old.nodes}
    removed = {n for n in old.nodes if n not in new.nodes}
    renames = _renames(old, new, removed, added)
    out: list[Finding] = []
    for before, after in sorted(renames.items()):
        out.append(Finding("node_renamed", f"{before} -> {after}", f"{new.nodes[after]}"))
    out += [Finding("node_added", n, new.nodes[n]) for n in sorted(added - set(renames.values()))]
    out += [Finding("node_removed", n, old.nodes[n]) for n in sorted(removed - set(renames))]
    moved = {_rename(e, renames) for e in old.edges}
    out += [
        Finding("edge_added", " ".join(e), "new relationship") for e in sorted(new.edges - moved)
    ]
    out += [
        Finding("edge_removed", " ".join(e), "no longer found")
        for e in sorted(old.edges)
        if _rename(e, renames) not in new.edges
    ]
    return out


def _renames(old: Snapshot, new: Snapshot, removed: set[str], added: set[str]) -> dict[str, str]:
    """``removed -> added`` pairs of one kind whose relationships mostly agree."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for before in sorted(removed):
        best, score = None, 0.0
        for after in sorted(added - taken):
            if old.nodes[before] != new.nodes[after]:
                continue
            a = _neighbours(old.edges, before)
            b = _neighbours(new.edges, after)
            if not a or not b:
                continue
            jaccard = len(a & b) / len(a | b)
            if jaccard > score:
                best, score = after, jaccard
        if best is not None and score >= _RENAME_MIN:
            out[before] = best
            taken.add(best)
    return out


def _neighbours(edges: Iterable[tuple[str, str, str]], node: str) -> set[str]:
    """What the node is connected to, a part (a schema) counted as its whole (its database).

    A rename can lose facts the docs state under the old name (``reads
    schema:crmdb.customers``) while the code still says ``uses database:crmdb``;
    both point at CRMDB.
    """
    edges = list(edges)
    whole = {src: dst for src, kind, dst in edges if kind == "part_of"}
    out = set()
    for src, kind, dst in edges:
        if kind == "part_of":
            continue
        if src == node:
            out.add(whole.get(dst, dst))
        elif dst == node:
            out.add(whole.get(src, src))
    return out


def _rename(edge: tuple[str, str, str], renames: Mapping[str, str]) -> tuple[str, str, str]:
    src, kind, dst = edge
    return (renames.get(src, src), kind, renames.get(dst, dst))


def _classes(old: Snapshot, new: Snapshot, grew: float) -> list[Finding]:
    out: list[Finding] = []
    for cid in sorted(set(new.classes) - set(old.classes)):
        c = new.classes[cid]
        detail = f"{c.incidents} incidents, {c.share:.0%} of the weight"
        if c.cause is None:
            continue  # the no-cause class is compared below
        out.append(Finding("class_new", cid, detail))
        if c.cause not in new.known_causes:
            out.append(Finding("class_uncovered", cid, f"no detected plugin declares {c.cause}"))
    for cid in sorted(set(old.classes) - set(new.classes)):
        out.append(Finding("class_gone", cid, f"was {old.classes[cid].share:.0%} of the weight"))
    for cid in sorted(set(old.classes) & set(new.classes)):
        a, b = old.classes[cid], new.classes[cid]
        if b.cause is None:
            if b.incidents > a.incidents:
                out.append(
                    Finding("uncaused_grew", cid, f"{a.incidents} -> {b.incidents} incidents")
                )
        elif b.share - a.share >= grew or (
            b.incidents - a.incidents >= _GREW_MIN and b.incidents >= 1.5 * a.incidents
        ):
            out.append(
                Finding(
                    "class_grew",
                    cid,
                    f"{a.incidents} -> {b.incidents} incidents, "
                    f"{a.share:.0%} -> {b.share:.0%} of the weight",
                )
            )
    new_uncaused = [
        cid for cid in set(new.classes) - set(old.classes) if new.classes[cid].cause is None
    ]
    out += [
        Finding("uncaused_grew", cid, f"{new.classes[cid].incidents} incidents with no cause")
        for cid in sorted(new_uncaused)
    ]
    return out


__all__ = ["ACTIONS", "ClassShape", "Finding", "FindingKind", "Snapshot", "diff"]
