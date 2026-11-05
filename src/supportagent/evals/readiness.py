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

The classifier's calibration is fitted from the replays (WP-A5).

**Shadow replay** (WP-O6c, :func:`apply_shadow`) then sends the same
incidents, in the order they were opened, through the real triage pipeline in
shadow: dedup, storm grouping, the pre-classifier, the fitted calibration, the
severity cap and routing by readiness. A class the lab proved stays ready only
when shadow routes ``min_routed`` of its incidents with its label, each on its
own case. Until it runs, the report says shadow replay wasn't measured.

**Proven live** (WP-O6c): an incident with no replayable evidence (its cause is
visible only live) counts once the agent's own RCA saw it live, in shadow. The
caller passes that RCA's result as a :class:`Replayed` with ``live`` set.

Pure: the caller runs the scenarios and passes the scores.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from supportagent.domain.classification import MIN_CALIBRATION_N, prefix_match
from supportagent.domain.gap_plan import target_name
from supportagent.domain.graph import SystemGraph
from supportagent.domain.incident_map import IncidentClass, IncidentMap, PastIncident
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.plugin import cause_hypothesis
from supportagent.domain.model.readiness import ReadinessGates
from supportagent.domain.stats import wilson_lower_bound
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
    #: ADR-0018: of those, ``(incident, why)`` when capture said why (``live_only``,
    #: ``aged_out``, ``unavailable``, ``failed``).
    unreplayed: tuple[tuple[str, str], ...] = ()
    #: Per incident: what the tools supported (``None``: nothing) and pass/fail.
    outcomes: tuple[tuple[str, str | None, bool], ...] = ()
    #: WP-O6c: incidents whose result is the agent's live RCA in shadow, not a replay.
    proven_live: tuple[str, ...] = ()
    #: WP-O6c: what shadow replay did with each of its incidents (``None``: not run).
    shadow: tuple[ShadowCase, ...] | None = None
    #: WP-O6c: the lab's verdict before shadow's gate (``None``: shadow didn't run).
    #: The lab baseline compares this: it guards what the lab proves, while
    #: ``status`` says whether the pipeline would route the class too.
    lab_status: Status | None = None

    @property
    def top1(self) -> float | None:
        return self.correct / self.scenarios if self.scenarios else None

    @property
    def routed(self) -> int | None:
        """WP-O6c: its incidents shadow routed with its label, on their own case."""
        if self.shadow is None:
            return None
        return sum(c.routes(self.label or _NO_CAUSE_LABEL) for c in self.shadow)


@dataclass(frozen=True, slots=True)
class ShadowCase:
    """WP-O6c: one past incident as the shadow pipeline triaged it."""

    incident: str
    #: ``routed`` or ``safe_default``, and the decision's reason (``routed`` when routed).
    outcome: str
    reason: str
    classification: str | None
    route: str | None = None
    #: The pre-classifier's label (which calibration row scored it).
    preclass: str | None = None
    #: The earlier incident whose case this one landed on (dedup), if any.
    merged_into: str | None = None
    #: The action kinds shadow would have proposed.
    actions: tuple[str, ...] = ()

    def routes(self, label: str) -> bool:
        return self.outcome == "routed" and self.classification == label and not self.merged_into


@dataclass
class ReadinessReport:
    system: str
    model_id: str
    bar: float
    classes: list[ClassReadiness] = field(default_factory=list)
    not_measured: tuple[str, ...] = ("shadow replay",)
    #: WP-A5: the classifier's calibration fitted from these replays.
    calibration: CalibrationTable | None = None
    #: WP-O6c: services the history hits that the catalog doesn't route
    #: (``(service, why)``). Configuration to complete, not a class failing.
    catalog_gaps: tuple[tuple[str, str], ...] = ()

    @property
    def ready_share(self) -> float:
        """The weight of ready classes **with a cause** (ADR-0017). Abstaining on the
        no-cause class is right, but it proves nothing the agent can support: a run
        whose readings all failed would otherwise look ready."""
        return round(
            sum(c.share for c in self.classes if c.status == "ready" and c.cause is not None), 3
        )

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
            routed = f"{c.routed}/{len(c.shadow)}" if c.shadow else "-"
            why = f": {'; '.join(c.reasons)}" if c.reasons else ""
            cause = c.cause or "(no cause)"
            out.append(f"  {c.share:>4.0%}  {c.status:<10} {cause:<32} {top1:>5} {routed:>5}{why}")
        for service, why in self.catalog_gaps:
            out.append(f"  catalog: {service}: {why}")
        if self.not_measured:
            out.append(f"  not measured yet: {', '.join(self.not_measured)}")
        return out

    def statuses(self) -> dict[str, str]:
        """Each class's lab verdict: what the readiness baseline guards (WP-O6c)."""
        return {c.class_id: c.lab_status or c.status for c in self.classes}

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
            "calibration": self.calibration.model_dump(mode="json") if self.calibration else None,
            "catalog_gaps": [list(g) for g in self.catalog_gaps],
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
                    "unreplayed": [list(u) for u in c.unreplayed],
                    "outcomes": [
                        {"incident": n, "supported": s, "passed": ok} for n, s, ok in c.outcomes
                    ],
                    "proven_live": list(c.proven_live),
                    "routed": c.routed,
                    "lab_status": c.lab_status,
                    "shadow": None
                    if c.shadow is None
                    else [
                        {
                            "incident": s.incident,
                            "outcome": s.outcome,
                            "reason": s.reason,
                            "classification": s.classification,
                            "route": s.route,
                            "preclass": s.preclass,
                            "merged_into": s.merged_into,
                            "actions": list(s.actions),
                        }
                        for s in c.shadow
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
#: What a component touches; its target (and the target's container) adds to its stack.
#: Not ``calls`` or ``depends_on``: another service's stack isn't this one's.
_FOLLOWED = frozenset({"uses", "reads", "writes", "consumes", "publishes", "runs_on"})


def service_entry(graph: SystemGraph, ci: str) -> dict[str, Any]:
    """The catalog entry the graph implies for ``ci``: its stack, databases and namespace.

    The stack is the node's technology plus that of what it uses, reads, writes,
    consumes from, publishes to and runs on, and of what those are part of (a
    schema's database, a queue's broker): the packs a case on it should use.
    The graph keeps the finer edge (``publishes topic:x``) and drops the coarse
    one (``uses runtime:rabbitmq/mq``), so the container is reached through it.
    A CI the graph doesn't know gets just its name.
    """
    name = ci if ci[:1].isalnum() else f"ci-{ci}"
    node = next(
        (n for n in graph.nodes.values() if n.kind in _SERVING and target_name(n.id, n.tech) == ci),
        None,
    )
    entry: dict[str, Any] = {"name": name, "stack": []}
    if node is None:
        return entry
    stack = set(node.tech)
    databases: list[str] = []
    part_of = {a: b for a, rel, b in graph.edges if rel == "part_of"}
    for src, relation, dst in sorted(graph.edges):
        target = graph.nodes.get(dst)
        if src != node.id or target is None or relation not in _FOLLOWED:
            continue
        stack |= target.tech
        container = graph.nodes.get(part_of.get(dst, ""))
        if container is not None:
            stack |= container.tech
        database = target if target.kind == "Database" else container
        name_ = target_name(database.id, database.tech) if database is not None else None
        if (
            relation != "runs_on"
            and database is not None
            and database.kind == "Database"
            and name_ not in databases
        ):
            databases.append(str(name_))
        if relation == "runs_on" and "kubernetes" in target.tech:
            entry["k8s_namespace"] = target_name(dst, target.tech).rsplit("/", 1)[-1]
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
            # ADR-0018: an empty world is a capture that couldn't read the cause's probes
            if not world and cls.cause is not None:
                skipped.append(number)
                continue
            scenario = _scenario(cls, incident, graph, world or {}, system)
            out.append(HistoryScenario(cls.id, number, scenario))
        missing[cls.id] = tuple(skipped)
    return out, missing


def incident_scenario(
    cls: IncidentClass,
    incident: PastIncident,
    graph: SystemGraph,
    world: Mapping[str, WorldEntry],
    system: str,
) -> LabScenario:
    """The lab scenario of one past incident of ``cls``, with the evidence in ``world``."""
    return _scenario(cls, incident, graph, world, system)


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
    #: WP-A5: what the classifier said, and the pre-classifier's label (calibration).
    predicted: str | None = None
    preclass: str | None = None
    #: WP-O6c: the agent's RCA on the live incident, in shadow; not a replay.
    live: bool = False


def fit_calibration(
    replayed: Iterable[Replayed],
    *,
    model_id: str,
    system: str,
    observed: Iterable[tuple[str | None, str | None, bool]] = (),
) -> CalibrationTable:
    """ADR-0017 (WP-A5): the classifier's calibration, fitted from the system's own history.

    One observation per replayed incident, keyed as the eval suites key them
    (ADR-0002): the predicted label's ``agree`` row (the pre-classifier's label
    matched) or ``abstain`` row (it had none); a conflict never routes, so it
    isn't evidence. Correct means the label onboarding mapped (or an
    acceptable one) matched. Graded ``holdout``: the history was never tuned on.

    ``observed`` (WP-O6c): ``(predicted, preclass, correct)`` for each resolved
    live case, triaged by the real pipeline. This is how a label's calibration
    grows past what the history alone holds. The caller leaves out the replays
    of incidents it observed live, so no incident counts twice.
    """
    counts: dict[str, dict[str, list[int]]] = {}
    rows = [(r.predicted, r.preclass, r.score.classification_ok) for r in replayed]
    for predicted, preclass, correct in [*rows, *observed]:
        if predicted is None:
            continue
        if preclass is None:
            row = "abstain"
        elif prefix_match(preclass, predicted):
            row = "agree"
        else:
            continue
        cell = counts.setdefault(predicted, {"agree": [0, 0], "abstain": [0, 0]})[row]
        cell[0] += 1
        cell[1] += int(correct)
    labels = {
        label: CalibrationLabelRow(
            agree=CalibrationCountRow(n=rows["agree"][0], correct=rows["agree"][1])
            if rows["agree"][0]
            else None,
            abstain=CalibrationCountRow(n=rows["abstain"][0], correct=rows["abstain"][1])
            if rows["abstain"][0]
            else None,
        )
        for label, rows in sorted(counts.items())
    }
    return CalibrationTable(
        version=1,
        grade="holdout",
        snapshot_id=f"history-{system}",
        model_id=model_id,
        labels=labels,
    ).signed(f"readiness-{system}")


def assess(  # noqa: PLR0913 — the run's inputs
    incident_map: IncidentMap,
    replayed: Mapping[str, Sequence[Replayed]],
    missing: Mapping[str, Sequence[str]],
    *,
    testable: Callable[[str], str | None],
    gates: ReadinessGates,
    system: str,
    model_id: str,
    unreplayed: Mapping[str, str] | None = None,
) -> ReadinessReport:
    """Each class's replayed scenarios against the gates.

    ``testable(cause)`` is ``None`` when something can test the cause, otherwise why not.
    ``unreplayed`` (ADR-0018): per incident, why capture left it without a world.
    """
    report = ReadinessReport(system=system, model_id=model_id, bar=gates.bar)
    why = unreplayed or {}
    for cls in incident_map.classes:
        lost = missing.get(cls.id, ())
        report.classes.append(
            _assess_class(
                cls,
                replayed.get(cls.id, ()),
                lost,
                testable,
                gates,
                unreplayed=tuple((n, why[n]) for n in lost if n in why),
            )
        )
    report.classes.sort(key=lambda c: (-c.share, c.class_id))
    return report


def _assess_class(  # noqa: PLR0913 — the class, its replays and what judges them
    cls: IncidentClass,
    replayed: Sequence[Replayed],
    missing: Sequence[str],
    testable: Callable[[str], str | None],
    gates: ReadinessGates,
    *,
    unreplayed: tuple[tuple[str, str], ...] = (),
) -> ClassReadiness:
    hypothesis = cause_hypothesis(cls.cause) if cls.cause else None
    p1 = cls.severities.get("P1", 0)
    why_not = testable(cls.cause) if cls.cause is not None else None
    if why_not is None and not replayed and missing and len(unreplayed) == len(missing):
        # ADR-0018: nothing replayable, and capture said why for every incident
        if all(w == "live_only" for _, w in unreplayed):
            why_not = (
                "its cause is visible only live (point-in-time tools can't read the past): "
                "prove it in shadow"
            )
        elif all(w == "aged_out" for _, w in unreplayed):
            why_not = "its evidence aged out of the stores' retention"
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
            unreplayed=unreplayed,
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
            + (f" ({_without(missing, unreplayed)})" if missing else "")
        )
    if len(false) > gates.max_false_causes:
        named = ", ".join(f"{n}: {sup}" for n, sup in false)
        reasons.append(f"{len(false)} false cause(s): {named}")
    if replayed and correct / len(replayed) < gates.min_top1:
        what = "abstained" if cls.cause is None else "found the cause"
        reasons.append(f"{what} below the {gates.min_top1:.0%} needed")
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
        unreplayed=unreplayed,
        outcomes=outcomes,
        proven_live=tuple(r.incident for r in replayed if r.live),
    )


# --- WP-O6c: shadow replay ----------------------------------------------------------------


def apply_shadow(  # noqa: PLR0913 — the report, shadow's cases and what judges them
    report: ReadinessReport,
    incident_map: IncidentMap,
    cases: Sequence[ShadowCase],
    *,
    gates: ReadinessGates,
    min_score: float,
    catalog_gaps: Sequence[tuple[str, str]] = (),
) -> ReadinessReport:
    """The report with each class's shadow replay, and its gate applied.

    A class the lab made ready stays ready only when shadow routed ``min_routed``
    of its incidents with its label, each on its own case. One that lands on an
    earlier incident's case was merged by dedup: the pipeline would never have
    triaged it. The other classes keep their status; their shadow is reported.
    """
    by_incident = {c.incident: c for c in cases}
    out: list[ClassReadiness] = []
    for cls in report.classes:
        members = next((m.incidents for m in incident_map.classes if m.id == cls.class_id), ())
        shadow = tuple(by_incident[n] for n in members if n in by_incident)
        measured = replace(cls, shadow=shadow, lab_status=cls.status)
        if cls.status == "ready" and shadow:
            why = _shadow_failure(measured, gates, report.calibration, min_score)
            if why is not None:
                measured = replace(measured, status="not_ready", reasons=(*cls.reasons, why))
        out.append(measured)
    return replace(
        report,
        classes=out,
        not_measured=tuple(m for m in report.not_measured if m != "shadow replay"),
        catalog_gaps=tuple(catalog_gaps),
    )


def _shadow_failure(
    cls: ClassReadiness,
    gates: ReadinessGates,
    calibration: CalibrationTable | None,
    min_score: float,
) -> str | None:
    """Why shadow keeps a lab-ready class from being ready; ``None`` when it doesn't.

    A case held back by calibration says what it waits for: how many more
    incidents of its label, classified correctly, clear the floor (ADR-0002).
    Those come from live shadow, once each is resolved.
    """
    shadow = cls.shadow or ()
    routed = cls.routed or 0
    if routed / len(shadow) >= gates.min_routed:
        return None
    label = cls.label or _NO_CAUSE_LABEL
    counts: dict[str, int] = {}
    for case in shadow:
        if case.routes(label):
            continue
        if case.merged_into:
            why = "merged into an earlier case"
        elif case.outcome == "routed":
            why = f"routed as {case.classification}"
        else:
            why = case.reason
        counts[why] = counts.get(why, 0) + 1
    detail = ", ".join(f"{n} {w}" for w, n in sorted(counts.items()))
    need = f"{gates.min_routed:.0%}"
    out = f"shadow routed {routed}/{len(shadow)}, below the {need} needed ({detail})"
    learning = sorted(
        {
            _learning(c.classification, c.preclass, calibration, min_score)
            for c in shadow
            if c.reason == "low-calibrated-score" and c.classification == label
        }
    )
    return out + "".join(f"; {line}" for line in learning)


def _learning(
    label: str | None, preclass: str | None, table: CalibrationTable | None, min_score: float
) -> str:
    """``learning live: database/latency needs 9 more correct (7/7 so far)``."""
    agree = preclass is not None and label is not None and prefix_match(preclass, label)
    row = table.labels.get(label or "") if table is not None else None
    count = (row.agree if agree else row.abstain) if row is not None else None
    n, correct = (count.n, count.correct) if count is not None else (0, 0)
    more = observations_needed(n, correct, min_score)
    which = "agreeing with the pre-classifier" if agree else "with no pre-classifier label"
    so_far = f"{correct}/{n} so far" if n else "none so far"
    if more is None:
        return f"learning live: {label} ({which}) can't reach the {min_score:.2f} floor"
    return (
        f"learning live: {label} ({which}) needs {more} more correctly classified "
        f"incident(s) ({so_far})"
    )


def observations_needed(
    n: int, correct: int, min_score: float, *, limit: int = 10_000
) -> int | None:
    """ADR-0002: how many more correct observations lift a label's row over ``min_score``.

    The row needs ``MIN_CALIBRATION_N`` observations and a Wilson lower bound
    of at least ``min_score``; each new one is assumed correct, so this is the
    fewest it could take. ``None`` when no number would (a floor of 1).
    """
    for more in range(limit + 1):
        total = n + more
        if total >= MIN_CALIBRATION_N and wilson_lower_bound(correct + more, total) >= min_score:
            return more
    return None


_UNREPLAYED = {
    "live_only": "visible only live",
    "aged_out": "aged out",
    "unavailable": "no tool here",
    "failed": "reads failed",
}


def _without(missing: Sequence[str], unreplayed: Sequence[tuple[str, str]]) -> str:
    """``3 without evidence: 2 visible only live, 1 aged out``."""
    counts: dict[str, int] = {}
    for _, why in unreplayed:
        counts[why] = counts.get(why, 0) + 1
    detail = ", ".join(f"{n} {_UNREPLAYED.get(w, w)}" for w, n in sorted(counts.items()))
    return f"{len(missing)} without evidence" + (f": {detail}" if detail else "")


__all__ = [
    "ClassReadiness",
    "HistoryScenario",
    "ReadinessReport",
    "Replayed",
    "ShadowCase",
    "apply_shadow",
    "assess",
    "fit_calibration",
    "history_scenarios",
    "incident_scenario",
    "observations_needed",
    "regressions",
    "service_entry",
]
