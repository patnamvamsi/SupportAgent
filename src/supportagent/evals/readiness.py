"""Readiness (ADR-0015 §5, WP-O6b): each incident class proven on the system's own history.

**Scenarios from history.** Each past incident in a class of the incident map
becomes a lab scenario (:mod:`supportagent.evals.lab`):

- the ticket, as it was opened;
- the service it hit, as the system graph knows it (the stack that selects
  the packs, its databases, its namespace);
- the truth onboarding mapped: the class's label, and its cause's hypothesis
  (``oracle/lock_contention`` -> ``oracle_lock_contention``); a class with no
  cause expects no cause claimed;
- the world: the evidence captured at the time. An incident with a cause but
  no evidence isn't replayed; it is counted, not guessed.

The lab then runs the real classifier and RCA on each. **Per class**:

- *top-1*: the share of its scenarios where the tools supported its cause;
- *false causes*: scenarios where they supported another (the strictest gate:
  any one keeps the class from being ready);
- for a class with no cause, *abstention*: no cause claimed.

A class is **ready** when it is testable (an evaluated plugin's pack, or the
common pack, has a probe for its cause), has enough scenarios, meets top-1
and has no false cause. The system is **ready to support** when its ready
classes hold ``bar`` of the history's incident weight (decision 6: 70%).

Calibration and shadow replay are not measured here yet; the report says so.
Pure: the caller runs the scenarios and passes the scores.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from supportagent.domain.gap_plan import target_name
from supportagent.domain.graph import SystemGraph
from supportagent.domain.incident_map import IncidentClass, IncidentMap, PastIncident
from supportagent.domain.model.plugin import cause_hypothesis
from supportagent.domain.model.readiness import ReadinessGates
from supportagent.evals.lab import LabScenario, LabScore, WorldEntry

Status = Literal["ready", "not_ready", "untestable"]
_NO_CAUSE_LABEL = "request/access"
_PRIORITY = {"P1": "1", "P2": "2", "P3": "3", "P4": "4"}


@dataclass(frozen=True, slots=True)
class HistoryScenario:
    class_id: str
    incident: str
    scenario: LabScenario


@dataclass(frozen=True, slots=True)
class ClassReadiness:
    class_id: str
    cause: str | None
    label: str | None
    hypothesis: str | None
    share: float
    p1: int
    status: Status
    reasons: tuple[str, ...]
    scenarios: int = 0
    correct: int = 0
    #: ``(incident, the hypothesis wrongly supported)``.
    false_causes: tuple[tuple[str, str], ...] = ()
    #: Incidents with a cause but no captured evidence: not replayed.
    no_evidence: tuple[str, ...] = ()
    #: Per incident: what the tools supported (``None``: nothing) and pass/fail.
    outcomes: tuple[tuple[str, str | None, bool], ...] = ()

    @property
    def top1(self) -> float | None:
        return self.correct / self.scenarios if self.scenarios else None


@dataclass
class ReadinessReport:
    system: str
    model_id: str
    bar: float
    classes: list[ClassReadiness] = field(default_factory=list)
    not_measured: tuple[str, ...] = ("calibration", "shadow replay")

    @property
    def ready_share(self) -> float:
        return round(sum(c.share for c in self.classes if c.status == "ready"), 3)

    @property
    def ready(self) -> bool:
        return self.ready_share >= self.bar

    @property
    def false_causes(self) -> int:
        return sum(len(c.false_causes) for c in self.classes)

    def lines(self) -> list[str]:
        """The report as the CLIs print it: the verdict, then one line per class."""
        verdict = "ready to support" if self.ready else "not ready to support"
        out = [
            f"{self.system}: {verdict}: ready classes hold {self.ready_share:.0%} of the "
            f"incident weight (bar {self.bar:.0%}); {self.false_causes} false cause(s)",
        ]
        for c in self.classes:
            top1 = f"{c.correct}/{c.scenarios}" if c.scenarios else "-"
            why = f": {'; '.join(c.reasons)}" if c.reasons else ""
            cause = c.cause or "(no cause)"
            out.append(f"  {c.share:>4.0%}  {c.status:<10} {cause:<32} {top1:>5}{why}")
        out.append(f"  not measured yet: {', '.join(self.not_measured)}")
        return out

    def statuses(self) -> dict[str, str]:
        return {c.class_id: c.status for c in self.classes}

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "system": self.system,
            "model_id": self.model_id,
            "bar": self.bar,
            "ready_share": self.ready_share,
            "ready": self.ready,
            "false_causes": self.false_causes,
            "not_measured": list(self.not_measured),
            "classes": [
                {
                    "id": c.class_id,
                    "cause": c.cause,
                    "label": c.label,
                    "hypothesis": c.hypothesis,
                    "share": c.share,
                    "p1": c.p1,
                    "status": c.status,
                    "reasons": list(c.reasons),
                    "scenarios": c.scenarios,
                    "correct": c.correct,
                    "top1": round(t, 3) if (t := c.top1) is not None else None,
                    "false_causes": [list(f) for f in c.false_causes],
                    "no_evidence": list(c.no_evidence),
                    "outcomes": [
                        {"incident": n, "supported": s, "passed": ok} for n, s, ok in c.outcomes
                    ],
                }
                for c in self.classes
            ],
        }


def regressions(report: ReadinessReport, baseline: Mapping[str, str]) -> list[str]:
    """Classes the baseline had ready that aren't now (the lab's ``no_regressions``)."""
    now = report.statuses()
    return sorted(
        c for c, status in baseline.items() if status == "ready" and now.get(c) != "ready"
    )


# --- the service each incident hit, from the graph ----------------------------------------

_SERVING = frozenset({"Service", "Job", "UI"})


def service_entry(graph: SystemGraph, ci: str) -> dict[str, Any]:
    """The catalog entry the graph implies for ``ci``: its stack, databases and namespace.

    The stack is the node's technology plus that of what it ``uses`` and
    ``runs_on`` (an Oracle database, a Kubernetes runtime): the packs a case
    on it should use. A CI the graph doesn't know gets just its name.
    """
    name = ci if ci[:1].isalnum() else f"ci-{ci}"
    node = next(
        (n for n in graph.nodes.values() if n.kind in _SERVING and target_name(n.id) == ci), None
    )
    entry: dict[str, Any] = {"name": name, "stack": []}
    if node is None:
        return entry
    stack = set(node.tech)
    databases: list[str] = []
    for src, relation, dst in sorted(graph.edges):
        target = graph.nodes.get(dst)
        if src != node.id or target is None or relation not in ("uses", "runs_on"):
            continue
        stack |= target.tech
        if relation == "uses" and target.kind == "Database":
            databases.append(target_name(dst))
        if relation == "runs_on" and "kubernetes" in target.tech:
            entry["k8s_namespace"] = target_name(dst).rsplit("/", 1)[-1]
            entry["k8s_selector"] = f"app={name}"
    entry["stack"] = sorted(stack)
    if databases:
        entry["databases"] = databases
    return entry


# --- scenarios -------------------------------------------------------------------------------


def history_scenarios(
    incident_map: IncidentMap,
    incidents: Sequence[PastIncident],
    graph: SystemGraph,
    evidence: Mapping[str, Mapping[str, WorldEntry]],
    *,
    system: str,
) -> tuple[list[HistoryScenario], dict[str, tuple[str, ...]]]:
    """One scenario per past incident of each class; and, per class, those not replayed.

    An incident in a class with a cause needs evidence to be replayed; one in
    a class with no cause is replayed as it is (abstaining needs no evidence).
    """
    by_number = {i.number: i for i in incidents}
    out: list[HistoryScenario] = []
    missing: dict[str, tuple[str, ...]] = {}
    for cls in incident_map.classes:
        skipped = []
        for number in cls.incidents:
            incident = by_number.get(number)
            if incident is None:
                continue
            world = evidence.get(number)
            if world is None and cls.cause is not None:
                skipped.append(number)
                continue
            scenario = _scenario(cls, incident, graph, world or {}, system)
            out.append(HistoryScenario(cls.id, number, scenario))
        missing[cls.id] = tuple(skipped)
    return out, missing


def _scenario(
    cls: IncidentClass,
    incident: PastIncident,
    graph: SystemGraph,
    world: Mapping[str, WorldEntry],
    system: str,
) -> LabScenario:
    hypothesis = cause_hypothesis(cls.cause) if cls.cause else None
    truth: dict[str, Any] = {
        "classification": cls.label or _NO_CAUSE_LABEL,
        "severity": incident.severity,
        "root_cause": hypothesis,
        "verdict": "SUPPORTED" if hypothesis else "CONSISTENT_UNCONFIRMED",
    }
    return LabScenario.model_validate(
        {
            "id": incident.number.lower(),
            "pack": system,
            "description": f"{incident.number} in class {cls.id}, replayed from history.",
            "incident": {
                "subject": incident.title or incident.number,
                "body": incident.description,
                "priority": _PRIORITY.get(incident.severity, "3"),
                "origin": "ticket",
            },
            "service": service_entry(graph, incident.ci),
            "world": dict(world),
            "truth": truth,
            "why": f"Onboarding mapped {incident.number} to {cls.id} ({cls.cause or 'no cause'}).",
        }
    )


# --- per class -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Replayed:
    """One scenario's result: what the tools supported (``None``: nothing) and its score."""

    incident: str
    supported: str | None
    score: LabScore


def assess(  # noqa: PLR0913 — the run's inputs
    incident_map: IncidentMap,
    replayed: Mapping[str, Sequence[Replayed]],
    missing: Mapping[str, Sequence[str]],
    *,
    testable: Callable[[str], str | None],
    gates: ReadinessGates,
    system: str,
    model_id: str,
) -> ReadinessReport:
    """Each class's replayed scenarios against the gates.

    ``testable(cause)`` is ``None`` when something can test the cause, otherwise why not.
    """
    report = ReadinessReport(system=system, model_id=model_id, bar=gates.bar)
    for cls in incident_map.classes:
        report.classes.append(
            _assess_class(cls, replayed.get(cls.id, ()), missing.get(cls.id, ()), testable, gates)
        )
    report.classes.sort(key=lambda c: (-c.share, c.class_id))
    return report


def _assess_class(
    cls: IncidentClass,
    replayed: Sequence[Replayed],
    missing: Sequence[str],
    testable: Callable[[str], str | None],
    gates: ReadinessGates,
) -> ClassReadiness:
    hypothesis = cause_hypothesis(cls.cause) if cls.cause else None
    p1 = cls.severities.get("P1", 0)
    why_not = testable(cls.cause) if cls.cause is not None else None
    if why_not is not None:
        return ClassReadiness(
            cls.id,
            cls.cause,
            cls.label,
            hypothesis,
            cls.share,
            p1,
            "untestable",
            (why_not,),
            no_evidence=tuple(missing),
        )
    outcomes = tuple(
        (r.incident, r.supported, r.score.root_cause_ok and not r.score.false_cause)
        for r in replayed
    )
    false = tuple(
        (r.incident, r.supported) for r in replayed if r.score.false_cause and r.supported
    )
    correct = sum(ok for _, _, ok in outcomes)
    reasons = []
    if len(replayed) < gates.min_scenarios:
        reasons.append(
            f"{len(replayed)} scenario(s) with evidence; needs {gates.min_scenarios}"
            + (f" ({len(missing)} without evidence)" if missing else "")
        )
    if len(false) > gates.max_false_causes:
        named = ", ".join(f"{n}: {sup}" for n, sup in false)
        reasons.append(f"{len(false)} false cause(s): {named}")
    if replayed and correct / len(replayed) < gates.min_top1:
        what = "abstained" if cls.cause is None else "found the cause"
        reasons.append(f"{what} in {correct} of {len(replayed)} (needs {gates.min_top1:.0%})")
    return ClassReadiness(
        cls.id,
        cls.cause,
        cls.label,
        hypothesis,
        cls.share,
        p1,
        "not_ready" if reasons else "ready",
        tuple(reasons),
        scenarios=len(replayed),
        correct=correct,
        false_causes=false,
        no_evidence=tuple(missing),
        outcomes=outcomes,
    )


__all__ = [
    "ClassReadiness",
    "HistoryScenario",
    "ReadinessReport",
    "Replayed",
    "assess",
    "history_scenarios",
    "regressions",
    "service_entry",
]
