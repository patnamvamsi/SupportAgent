"""The onboarding lab (ADR-0015, WP-O7): does an instance understand a system it is pointed at?

A **reference system** is everything an instance would be handed for a real
system, plus the answers:

- ``system.yaml``: the onboarding manifest;
- ``sources/``: Confluence pages (some stale, some wrong, one injected), the
  ServiceNow CMDB, incidents, problems and changes, repository manifests, and
  what read-only live discovery returns;
- ``truth.yaml``: the system graph, the plugins it needs, the access requests
  it should make, and what it must *not* assert or request;
- ``truth_incidents.yaml``: the incident classes, written by the generator.

Scoring is structural, like the incident lab: an :class:`OnboardingResult`
(what an onboarding run produced) is compared with the truth, stage by stage
(O1 graph, O2 incident map, O3 plugins, O4 requests). The strictest checks are
the ``forbidden`` counts: a fact from a superseded page, a plugin for another
stack, a request beyond read-only. Each must be zero.

Until the pipeline stages land (WP-O1..O6), the lab runs two fixed results:
**oracle** (the truth itself, which must pass every gate) and **null** (an
empty result, which must fail every stage). Together they prove the scorer
and the gates bite. A stage's gates apply to the real pipeline once it is in
``enforced_stages``.

Pure: no I/O. ``bootstrap.load_reference_system`` reads the files.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Stage = Literal["graph", "incidents", "plugins", "requests"]
STAGES: tuple[Stage, ...] = ("graph", "incidents", "plugins", "requests")
#: ARCH §11 severity weights, for an incident class's share of the history.
PRIORITY_WEIGHT: Mapping[str, int] = {"1": 8, "2": 4, "3": 2, "4": 1, "5": 1}
_SOURCE = re.compile(r"^(confluence|git|cmdb|live):(.+)$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- the truth ------------------------------------------------------------------------


class TruthNode(_Strict):
    id: str = Field(pattern=r"^[a-z]+:.+$")
    kind: str
    tech: list[str] = Field(default_factory=list)
    sources: list[str] = Field(min_length=1)


class TruthEdge(_Strict):
    src: str
    kind: str
    dst: str
    sources: list[str] = Field(min_length=1)


class ForbiddenNode(_Strict):
    id: str
    why: str


class ForbiddenEdge(_Strict):
    src: str
    kind: str
    #: May be a glob (``schema:paydb.*``).
    dst: str
    why: str


class Forbidden(_Strict):
    nodes: list[ForbiddenNode] = Field(default_factory=list)
    edges: list[ForbiddenEdge] = Field(default_factory=list)


class Question(_Strict):
    about: str
    sources: list[str] = Field(min_length=2)


class TruthGraph(_Strict):
    nodes: list[TruthNode] = Field(min_length=1)
    edges: list[TruthEdge] = Field(min_length=1)
    must_not: Forbidden = Field(default_factory=Forbidden)
    questions: list[Question] = Field(default_factory=list)


class TruthPlugins(_Strict):
    required: list[str] = Field(min_length=1)
    must_not: list[str] = Field(default_factory=list)


class RequestKey(_Strict):
    plugin: str
    access: str
    target: str


class TruthRequests(_Strict):
    expected: list[RequestKey] = Field(min_length=1)
    forbidden_scope: list[str] = Field(default_factory=list)


class TruthClass(_Strict):
    id: str
    #: The preferred taxonomy label first, then acceptable ones.
    labels: list[str] = Field(min_length=1)
    #: The hypothesis a good onboarding maps it to; ``None`` for noise.
    cause: str | None
    incidents: list[str] = Field(min_length=1)


class Truth(_Strict):
    graph: TruthGraph
    plugins: TruthPlugins
    requests: TruthRequests
    incident_classes: list[TruthClass] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Truth:
        ids = {n.id for n in self.graph.nodes}
        dangling = [e for e in self.graph.edges if e.src not in ids or e.dst not in ids]
        if dangling:
            raise ValueError(f"edges with unknown endpoints: {dangling[:3]}")  # noqa: TRY003
        seen: set[str] = set()
        for cls in self.incident_classes:
            twice = seen & set(cls.incidents)
            if twice:
                raise ValueError(f"incidents in two classes: {sorted(twice)}")  # noqa: TRY003
            seen |= set(cls.incidents)
        overlap = set(self.plugins.required) & set(self.plugins.must_not)
        if overlap:
            raise ValueError(f"plugins both required and forbidden: {sorted(overlap)}")  # noqa: TRY003
        return self


@dataclass(frozen=True, slots=True)
class Inventory:
    """What the sources contain, so every truth reference can be checked."""

    confluence: frozenset[str]  # page ids
    git: frozenset[str]  # <repo>/<path>
    cmdb: frozenset[str]  # CI and relationship sys_ids
    live: frozenset[str]  # discovery keys
    #: incident number -> priority
    incidents: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReferenceSystem:
    id: str
    name: str
    manifest: Mapping[str, object]
    truth: Truth
    inventory: Inventory

    def problems(self) -> list[str]:
        """Why the fixture can't be trusted as ground truth; empty when it can."""
        out: list[str] = []
        refs = [(n.id, s) for n in self.truth.graph.nodes for s in n.sources]
        refs += [
            (f"{e.src} {e.kind} {e.dst}", s) for e in self.truth.graph.edges for s in e.sources
        ]
        refs += [(q.about, s) for q in self.truth.graph.questions for s in q.sources]
        for fact, ref in refs:
            if not self._resolves(ref):
                out.append(f"{fact}: source {ref!r} is not in the fixture")
        for cls in self.truth.incident_classes:
            missing = [i for i in cls.incidents if i not in self.inventory.incidents]
            if missing:
                out.append(f"class {cls.id}: incidents {missing} are not in incidents.yaml")
        classed = {i for c in self.truth.incident_classes for i in c.incidents}
        unclassed = sorted(set(self.inventory.incidents) - classed)
        if unclassed:
            out.append(f"incidents in no class: {unclassed[:5]}")
        truth_nodes = {n.id for n in self.truth.graph.nodes}
        out += [
            f"must_not node {f.id} is also a truth node"
            for f in self.truth.graph.must_not.nodes
            if f.id in truth_nodes
        ]
        truth_edges = {(e.src, e.kind, e.dst) for e in self.truth.graph.edges}
        out += [
            f"must_not edge {f.src} {f.kind} {f.dst} matches a truth edge"
            for f in self.truth.graph.must_not.edges
            if any(_forbidden_edge(f, edge) for edge in truth_edges)
        ]
        return out

    def _resolves(self, ref: str) -> bool:
        match = _SOURCE.match(ref)
        if match is None:
            return False
        kind, key = match.groups()
        pool: Collection[str] = getattr(self.inventory, kind)
        return key in pool

    def class_weights(self) -> dict[str, int]:
        """Each class's share of the history: the sum of its incidents' priority weights."""
        return {
            c.id: sum(PRIORITY_WEIGHT.get(self.inventory.incidents[i], 1) for i in c.incidents)
            for c in self.truth.incident_classes
        }


# --- what an onboarding run produced ---------------------------------------------------


class ResultNode(_Strict):
    id: str
    kind: str


class ResultEdge(_Strict):
    src: str
    kind: str
    dst: str


class ResultClass(_Strict):
    id: str
    label: str
    cause: str | None = None
    incidents: list[str]


class ResultRequest(_Strict):
    plugin: str
    access: str
    target: str
    #: The grant text the request asks for (the plugin's least-privilege contract).
    scope: str


class OnboardingResult(_Strict):
    nodes: list[ResultNode] = Field(default_factory=list)
    edges: list[ResultEdge] = Field(default_factory=list)
    incident_classes: list[ResultClass] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    requests: list[ResultRequest] = Field(default_factory=list)


def oracle(system: ReferenceSystem) -> OnboardingResult:
    """The truth as a result: it must pass every gate (the scorer's self-test)."""
    t = system.truth
    return OnboardingResult(
        nodes=[ResultNode(id=n.id, kind=n.kind) for n in t.graph.nodes],
        edges=[ResultEdge(src=e.src, kind=e.kind, dst=e.dst) for e in t.graph.edges],
        incident_classes=[
            ResultClass(id=c.id, label=c.labels[0], cause=c.cause, incidents=c.incidents)
            for c in t.incident_classes
        ],
        plugins=list(t.plugins.required),
        requests=[
            ResultRequest(
                plugin=r.plugin,
                access=r.access,
                target=r.target,
                scope=f"read-only: the {r.plugin} plugin's {r.access} contract",
            )
            for r in t.requests.expected
        ],
    )


def null() -> OnboardingResult:
    """An empty result: it must fail every stage."""
    return OnboardingResult()


# --- scoring --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageScore:
    stage: Stage
    metrics: Mapping[str, float]
    #: The worst offenders, for the report (a forbidden fact, a missed request).
    notes: Sequence[str] = ()


def score(system: ReferenceSystem, result: OnboardingResult) -> dict[Stage, StageScore]:
    return {
        "graph": _graph(system.truth.graph, result),
        "incidents": _incidents(system.truth.incident_classes, result),
        "plugins": _plugins(system.truth.plugins, result),
        "requests": _requests(system.truth.requests, result),
    }


def _pr(predicted: set[object], truth: set[object]) -> tuple[float, float]:
    hit = len(predicted & truth)
    precision = hit / len(predicted) if predicted else 0.0
    recall = hit / len(truth) if truth else 1.0
    return round(precision, 3), round(recall, 3)


def _forbidden_edge(f: ForbiddenEdge, edge: tuple[str, str, str]) -> bool:
    src, kind, dst = edge
    return src == f.src and kind == f.kind and fnmatch.fnmatchcase(dst, f.dst)


def _graph(truth: TruthGraph, result: OnboardingResult) -> StageScore:
    nodes = {n.id for n in result.nodes}
    edges = {(e.src, e.kind, e.dst) for e in result.edges}
    node_p, node_r = _pr(set(nodes), {n.id for n in truth.nodes})
    edge_p, edge_r = _pr(set(edges), {(e.src, e.kind, e.dst) for e in truth.edges})
    bad = [f"node {f.id}: {f.why}" for f in truth.must_not.nodes if f.id in nodes]
    bad += [
        f"edge {f.src} {f.kind} {f.dst}: {f.why}"
        for f in truth.must_not.edges
        if any(_forbidden_edge(f, e) for e in edges)
    ]
    return StageScore(
        "graph",
        {
            "node_precision": node_p,
            "node_recall": node_r,
            "edge_precision": edge_p,
            "edge_recall": edge_r,
            "forbidden": float(len(bad)),
        },
        bad,
    )


def _pairs(groups: Iterable[Iterable[str]]) -> set[frozenset[str]]:
    return {frozenset(p) for g in groups for p in combinations(sorted(set(g)), 2)}


def _incidents(truth: Sequence[TruthClass], result: OnboardingResult) -> StageScore:
    true_pairs = _pairs(c.incidents for c in truth)
    got_pairs = _pairs(c.incidents for c in result.incident_classes)
    p, r = _pr(set(got_pairs), set(true_pairs))
    f1 = round(2 * p * r / (p + r), 3) if p + r else 0.0
    assigned = {i: c for c in result.incident_classes for i in c.incidents}
    labelled = caused = with_cause = 0
    total = 0
    misses: list[str] = []
    for cls in truth:
        for number in cls.incidents:
            total += 1
            got = assigned.get(number)
            if got is not None and got.label in cls.labels:
                labelled += 1
            if cls.cause is not None:
                with_cause += 1
                if got is not None and got.cause == cls.cause:
                    caused += 1
                elif len(misses) < 5:  # noqa: PLR2004 — a short report
                    misses.append(f"{number} ({cls.id}): cause {got.cause if got else None}")
    return StageScore(
        "incidents",
        {
            "pair_f1": f1,
            "label_accuracy": round(labelled / total, 3) if total else 0.0,
            "cause_accuracy": round(caused / with_cause, 3) if with_cause else 1.0,
        },
        misses,
    )


def _plugins(truth: TruthPlugins, result: OnboardingResult) -> StageScore:
    got = set(result.plugins)
    p, r = _pr(set(got), set(truth.required))
    bad = sorted(got & set(truth.must_not))
    missing = sorted(set(truth.required) - got)
    notes = [f"for another stack: {p_}" for p_ in bad] + [f"missing: {m}" for m in missing]
    return StageScore("plugins", {"precision": p, "recall": r, "forbidden": float(len(bad))}, notes)


def _requests(truth: TruthRequests, result: OnboardingResult) -> StageScore:
    keys = {(q.plugin, q.access, q.target) for q in result.requests}
    p, r = _pr(set(keys), {(k.plugin, k.access, k.target) for k in truth.expected})
    patterns = [re.compile(x, re.IGNORECASE) for x in truth.forbidden_scope]
    bad = [
        f"{q.plugin}/{q.access} on {q.target}: {q.scope!r}"
        for q in result.requests
        if any(pat.search(q.scope) for pat in patterns)
    ]
    missing = sorted(
        f"{k.plugin}/{k.access} on {k.target}"
        for k in truth.expected
        if (k.plugin, k.access, k.target) not in keys
    )
    return StageScore(
        "requests",
        {"precision": p, "recall": r, "forbidden": float(len(bad))},
        bad + [f"missing: {m}" for m in missing],
    )


# --- gates ----------------------------------------------------------------------------


class OnboardingGates(_Strict):
    """``evals/onboarding/gates.yaml``: a floor (or, for ``forbidden``, a ceiling) per metric."""

    graph: dict[str, float]
    incidents: dict[str, float]
    plugins: dict[str, float]
    requests: dict[str, float]
    #: Stages whose gates bind the real pipeline; they land with WP-O1..O6.
    enforced_stages: list[Stage] = Field(default_factory=list)

    def for_stage(self, stage: Stage) -> Mapping[str, float]:
        gates: Mapping[str, float] = getattr(self, stage)
        return gates


@dataclass(frozen=True, slots=True)
class GateResult:
    stage: Stage
    metric: str
    ok: bool
    value: float
    bound: float

    @property
    def detail(self) -> str:
        sign = "<=" if self.metric == "forbidden" else ">="
        return f"{self.stage}.{self.metric} = {self.value:g} (needs {sign} {self.bound:g})"


def evaluate(scores: Mapping[Stage, StageScore], gates: OnboardingGates) -> list[GateResult]:
    out: list[GateResult] = []
    for stage in STAGES:
        metrics = scores[stage].metrics
        for metric, bound in gates.for_stage(stage).items():
            value = metrics[metric]
            ok = value <= bound if metric == "forbidden" else value >= bound
            out.append(GateResult(stage, metric, ok, value, bound))
    return out


def stage_passes(results: Sequence[GateResult], stage: Stage) -> bool:
    return all(r.ok for r in results if r.stage == stage)


__all__ = [
    "PRIORITY_WEIGHT",
    "STAGES",
    "GateResult",
    "Inventory",
    "OnboardingGates",
    "OnboardingResult",
    "ReferenceSystem",
    "ResultClass",
    "ResultEdge",
    "ResultNode",
    "ResultRequest",
    "StageScore",
    "Truth",
    "evaluate",
    "null",
    "oracle",
    "score",
    "stage_passes",
]
