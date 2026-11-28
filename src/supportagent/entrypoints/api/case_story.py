"""How the case page tells a case's story: the triage gates and the trace timeline.

**Gates.** Triage asks its questions in ARCH §4 order and stops at the first one a
case fails (``app/classify.py``). ``gates`` replays that order from the stored
decision: each question passed, the one it stopped at, or not reached. Three
reasons stop before the classifier runs at all (a storm member, the spend breaker,
no paging integration); they show as one stop before the first question.

**Timeline.** ``timeline`` lays the trace's events out for the replay: each with its
offset from the first event, its depth in the span tree, and the one-line summary
replay already gives (``app/replay.summarize``). Payloads are not shown here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from supportagent.app.replay import summarize
from supportagent.domain.model.event import Event

GateState = Literal["passed", "stopped", "routed", "not_reached"]

#: Reasons that stop a case before triage asks its first question.
_BEFORE = {
    "storm-member": "It arrived during an alert storm and joined the storm's case.",
    "spend-breaker": "The model budget for this period was spent, so the model wasn't asked.",
    "no-paging-integration": "Its service has no paging integration, so a person takes it.",
}
#: ARCH §4, in order: the reason a case stops at each question.
_QUESTIONS = (
    ("unresolved-service", "Is the service known?"),
    ("llm-failure", "Did the model answer?"),
    ("classifier-conflict", "Do the rules and the model agree?"),
    ("low-calibrated-score", "Is the label proven on this system?"),
    ("unconfirmed-high-severity", "Is a P1 or P2 cross-checked?"),
    ("class-not-ready", "Is this label ready to route?"),
    ("routed", "Route it"),
)


@dataclass(frozen=True, slots=True)
class Gate:
    key: str
    question: str
    state: GateState
    detail: str


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _passed(key: str, d: Mapping[str, object], service: str) -> str:  # noqa: PLR0911 — one line per question
    label, preclass = _text(d.get("classification")), _text(d.get("preclass_label"))
    severity, score = _text(d.get("severity")), d.get("calibrated_score")
    if key == "unresolved-service":
        return f"It belongs to {service}." if service else "The service was resolved."
    if key == "llm-failure":
        return f"The model labelled it {label}." if label else "The model answered."
    if key == "classifier-conflict":
        return (
            f"The rules also say {preclass}."
            if preclass
            else "No rule matched, so nothing contradicted the model."
        )
    if key == "low-calibrated-score":
        return f"Calibrated score {score:.2f}." if isinstance(score, float) else "Score cleared."
    if key == "unconfirmed-high-severity":
        if severity in ("P1", "P2"):
            return f"{severity}, and the rules confirmed the label."
        return f"{severity or 'Its severity'} needs no cross-check."
    return "This label's class is ready."


def _stopped(key: str, d: Mapping[str, object]) -> str:  # noqa: PLR0911 — one line per question
    label, preclass = _text(d.get("classification")), _text(d.get("preclass_label"))
    severity, score = _text(d.get("severity")), d.get("calibrated_score")
    if key == "unresolved-service":
        return "No catalog service matched the alert, so a person takes it."
    if key == "llm-failure":
        return "The model gave no usable answer, so a person takes it."
    if key == "classifier-conflict":
        return f"The rules say {preclass}, the model says {label}. A person decides."
    if key == "low-calibrated-score":
        shown = f"{score:.2f}" if isinstance(score, float) else "0"
        return (
            f"Calibrated score {shown} is under the floor: {label or 'this label'} "
            "hasn't been confirmed often enough on this system yet."
        )
    if key == "unconfirmed-high-severity":
        return f"{severity} with no rule confirming the label: the model's answer is a suggestion."
    if key == "class-not-ready":
        return "This system's history hasn't proved this label yet: a person takes it."
    route = _text(d.get("route_id"))
    return f"Routed on {route}." if route else "Routed."


def gates(decision: Mapping[str, object] | None, *, service: str = "") -> list[Gate]:
    """The questions triage asked of this case, in order, and where it stopped."""
    if not decision:
        return []
    reason = _text(decision.get("reason"))
    found: list[Gate] = []
    stopped = False
    if reason in _BEFORE:
        found.append(Gate(reason, "Can triage run?", "stopped", _BEFORE[reason]))
        stopped = True
    elif reason not in {key for key, _ in _QUESTIONS}:
        found.append(Gate(reason, "Triage stopped", "stopped", reason.replace("-", " ")))
        stopped = True
    for key, question in _QUESTIONS:
        if stopped:
            found.append(Gate(key, question, "not_reached", ""))
        elif key == reason:
            state: GateState = "routed" if key == "routed" else "stopped"
            found.append(Gate(key, question, state, _stopped(key, decision)))
            stopped = True
        else:
            found.append(Gate(key, question, "passed", _passed(key, decision, service)))
    return found


@dataclass(frozen=True, slots=True)
class Step:
    kind: str
    actor: str
    status: str
    at_ms: int
    latency_ms: int | None
    depth: int
    model: str | None
    tokens: int
    summary: str


def timeline(events: Iterable[Event]) -> list[Step]:
    """The trace's events for the replay, with offsets and span depth."""
    ordered = list(events)
    if not ordered:
        return []
    start = ordered[0].ts
    parent = {e.span_id: e.parent_span_id for e in ordered}

    def depth(span: str | None) -> int:
        seen = 0
        while span and parent.get(span) and seen < 8:  # noqa: PLR2004 — a bounded walk
            span = parent[span]
            seen += 1
        return seen

    return [
        Step(
            kind=e.kind.value,
            actor=e.actor,
            status=e.status,
            at_ms=int((e.ts - start).total_seconds() * 1000),
            latency_ms=e.latency_ms,
            depth=depth(e.span_id),
            model=e.model,
            tokens=(e.tokens_in or 0) + (e.tokens_out or 0),
            summary=summarize(e.kind, e.payload),
        )
        for e in ordered
    ]


__all__ = ["Gate", "Step", "gates", "timeline"]
