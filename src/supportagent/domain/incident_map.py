"""The incident map (ADR-0015 P3, WP-O4): what goes wrong in this system, and how often.

A system's ticket history becomes **incident classes**: groups of past
incidents with one cause, each mapped to a taxonomy label and weighted by
count x severity x time to resolve. The classes say which causes onboarding
must be able to test (the gap plan, WP-O5) and which it can already explain.

Structure first, as for the graph:

1. **The model reads each incident** against a catalogue of labels and causes
   (the built-in ones plus those of the plugins the system needs) and names a
   label, a cause or none, and the words in the ticket that show it. A reading
   is **grounded**: the label and cause are in the catalogue and the quote is
   in the ticket word for word; otherwise the cause is dropped.
2. **The ticket's own structure overrules a reading.** Incidents a person
   linked to one problem record are one class: the majority of their readings
   names it. An incident a change caused is a change-caused incident
   (``recent_change_push``), whatever its text suggests.
3. **Classes are causes.** Every incident with the same cause is one class;
   incidents with no cause group by label (routine requests, say) and count as
   not covered.

Pure: the connector turns its records into :class:`PastIncident` and the app
service calls the model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

#: The cause of an incident a change was recorded as causing.
CHANGE_CAUSE = "recent_change_push"
#: Built-in causes (``taxonomy/hypotheses.yaml``), described for the model.
BUILT_IN_CAUSES: Mapping[str, str] = {
    "recent_change_push": "A recent deploy, release or configuration change caused it; "
    "rolling it back fixed it.",
    "capacity_decay": "Load grew past what the component was sized for, gradually.",
    "dependency_failure": "An internal dependency the component calls was down or failing.",
    "upstream_provider": "A third-party provider had an outage or degradation on its side.",
    "traffic_spike": "A sudden burst of traffic beyond normal peaks.",
    "data_volume": "An unusually large volume of data in one run or request.",
}
#: Severity weights (ARCH §11): a P1 counts eight P4s.
SEVERITY_WEIGHT: Mapping[str, int] = {"P1": 8, "P2": 4, "P3": 2, "P4": 1}
_QUOTE_MIN = 8


@dataclass(frozen=True, slots=True)
class PastIncident:
    number: str
    opened: datetime
    resolved: datetime | None
    severity: str  # P1..P4
    ci: str
    title: str
    description: str
    resolution: str
    #: The problem record a person linked it to, and that problem's title.
    problem: str | None = None
    problem_title: str | None = None
    #: The change recorded as having caused it.
    caused_by_change: str | None = None

    @property
    def text(self) -> str:
        """Everything a reading may quote: the ticket and the problem record it links to."""
        return "\n".join((self.title, self.description, self.resolution, self.problem_title or ""))

    @property
    def hours_to_resolve(self) -> float | None:
        if self.resolved is None:
            return None
        return max((self.resolved - self.opened).total_seconds() / 3600, 0.0)


@dataclass(frozen=True, slots=True)
class Catalogue:
    """The labels and causes onboarding may map incidents to, with what each means."""

    labels: Mapping[str, str]
    causes: Mapping[str, str]

    def text(self) -> tuple[str, str]:
        return (
            "\n".join(f"- {k}: {v}" for k, v in sorted(self.labels.items())),
            "\n".join(f"- {k}: {v}" for k, v in sorted(self.causes.items())),
        )


def catalogue(labels: Iterable[tuple[str, str]], causes: Iterable[tuple[str, str]]) -> Catalogue:
    """Labels and causes in order of precedence; the first description of an id wins."""
    merged_labels: dict[str, str] = {}
    for label, text in labels:
        merged_labels.setdefault(label, " ".join(text.split()))
    merged_causes: dict[str, str] = dict(BUILT_IN_CAUSES)
    for cause, text in causes:
        merged_causes.setdefault(cause, " ".join(text.split()))
    return Catalogue(merged_labels, merged_causes)


@dataclass(frozen=True, slots=True)
class Reading:
    """The model's reading of one incident (``map_incidents``), before grounding."""

    number: str
    label: str | None
    cause: str | None
    quote: str = ""


@dataclass(frozen=True, slots=True)
class GroundedReading:
    number: str
    label: str | None
    cause: str | None
    quote: str | None
    #: Why a part of the reading was dropped, if one was.
    dropped: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.replace("`", "").replace("*", "").split()).lower()


def ground(reading: Reading, incident: PastIncident, cat: Catalogue) -> GroundedReading:
    """Keep the label if the catalogue has it, the cause if it also has a verbatim quote."""
    label = reading.label if reading.label in cat.labels else None
    why: list[str] = []
    if reading.label and label is None:
        why.append(f"label {reading.label} is not in the catalogue")
    cause = reading.cause or None
    quote = reading.quote.strip() or None
    if cause is not None:
        if cause not in cat.causes:
            why.append(f"cause {cause} is not in the catalogue")
            cause = None
        elif quote is None or len(_norm(quote)) < _QUOTE_MIN:
            why.append(f"cause {cause} has no usable quote")
            cause = None
        elif _norm(quote) not in _norm(incident.text):
            why.append(f"the quote for {cause} is not in the ticket")
            cause = None
    return GroundedReading(
        incident.number, label, cause, quote if cause else None, "; ".join(why) or None
    )


@dataclass(frozen=True, slots=True)
class IncidentClass:
    id: str
    label: str | None
    cause: str | None
    incidents: tuple[str, ...]
    problems: tuple[str, ...]
    changes: tuple[str, ...]
    #: count x severity x time to resolve, and this class's share of the history's.
    weight: float
    share: float
    mean_hours_to_resolve: float | None
    severities: Mapping[str, int]
    #: Up to three ticket quotes that show the cause.
    quotes: tuple[str, ...] = ()
    #: How the class was formed: by problem record, change link, or the readings.
    basis: tuple[str, ...] = ()


@dataclass(slots=True)
class IncidentMap:
    classes: list[IncidentClass] = field(default_factory=list)
    #: The share of incidents in a class with a cause the catalogue knows.
    coverage: float = 0.0
    readings: dict[str, GroundedReading] = field(default_factory=dict)
    #: Incidents the model gave no usable reading for.
    unread: list[str] = field(default_factory=list)


def _majority(values: Iterable[str | None]) -> str | None:
    counts = Counter(v for v in values if v is not None)
    if not counts:
        return None
    best = max(counts.values())
    return min(v for v, n in counts.items() if n == best)  # ties: the first id, for stability


def _weight(incident: PastIncident) -> float:
    hours = incident.hours_to_resolve
    return SEVERITY_WEIGHT.get(incident.severity, 1) * (1.0 + (hours or 0.0) / 24)


def build_map(
    incidents: Sequence[PastIncident], readings: Mapping[str, GroundedReading]
) -> IncidentMap:
    """Group the history into classes, structure first (see the module docstring)."""
    by_number = {i.number: i for i in incidents}
    cause: dict[str, str | None] = {n: r.cause for n, r in readings.items()}
    label: dict[str, str | None] = {n: r.label for n, r in readings.items()}
    basis: dict[str, str] = dict.fromkeys(by_number, "reading")
    problems: dict[str, list[str]] = {}
    for incident in incidents:
        if incident.problem:
            problems.setdefault(incident.problem, []).append(incident.number)
    for members in problems.values():
        shared_cause = _majority(cause.get(n) for n in members)
        shared_label = _majority(label.get(n) for n in members)
        for number in members:
            cause[number], label[number], basis[number] = shared_cause, shared_label, "problem"
    changed = [i.number for i in incidents if i.caused_by_change]
    change_label = _majority(label.get(n) for n in changed)
    for number in changed:
        cause[number], basis[number] = CHANGE_CAUSE, "change"
        label[number] = label.get(number) or change_label
    groups: dict[tuple[str, str], list[str]] = {}
    for incident in incidents:
        n = incident.number
        key = ("cause", c) if (c := cause.get(n)) else ("label", label.get(n) or "unknown")
        groups.setdefault(key, []).append(n)
    total = sum(_weight(i) for i in incidents) or 1.0
    classes = []
    for (kind, value), members in groups.items():
        rows = [by_number[n] for n in members]
        hours = [h for r in rows if (h := r.hours_to_resolve) is not None]
        weight = sum(_weight(r) for r in rows)
        classes.append(
            IncidentClass(
                id=value.replace("/", "-").replace("_", "-"),
                label=_majority(label.get(n) for n in members),
                cause=value if kind == "cause" else None,
                incidents=tuple(sorted(members)),
                problems=tuple(sorted({r.problem for r in rows if r.problem})),
                changes=tuple(sorted({r.caused_by_change for r in rows if r.caused_by_change})),
                weight=round(weight, 2),
                share=round(weight / total, 3),
                mean_hours_to_resolve=round(sum(hours) / len(hours), 1) if hours else None,
                severities=dict(sorted(Counter(r.severity for r in rows).items())),
                quotes=tuple(
                    dict.fromkeys(q for n in members if (q := _quote(readings, n)) is not None)
                )[:3],
                basis=tuple(sorted({basis[n] for n in members})),
            )
        )
    classes.sort(key=lambda c: (-c.weight, c.id))
    covered = sum(len(c.incidents) for c in classes if c.cause is not None)
    return IncidentMap(
        classes=classes,
        coverage=round(covered / len(incidents), 3) if incidents else 0.0,
        readings=dict(readings),
        unread=sorted(n for n in by_number if n not in readings),
    )


def _quote(readings: Mapping[str, GroundedReading], number: str) -> str | None:
    reading = readings.get(number)
    return reading.quote if reading is not None else None


__all__ = [
    "BUILT_IN_CAUSES",
    "CHANGE_CAUSE",
    "SEVERITY_WEIGHT",
    "Catalogue",
    "GroundedReading",
    "IncidentClass",
    "IncidentMap",
    "PastIncident",
    "Reading",
    "build_map",
    "catalogue",
    "ground",
]
