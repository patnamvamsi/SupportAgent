"""What a traced object's answer may say (ADR-0021 §5). Pure.

:func:`facts` turns a trace into numbered facts: one per stage (``S1``...),
the verdict (``V``), each pseudonymised evidence line (``E1``...) and each
cause RCA found on the stopping component (``C1``...). The model writes its
answer only over these, citing them; a sentence that cites nothing it was
given is dropped.

:func:`template_answer` is the answer with no model at all, from the verdict
alone. It is always available, and is what a reader gets when the model is
off, fails, or none of its sentences survive the checks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from supportagent.domain.fieldpath import values_at
from supportagent.domain.flow_trace import StageObservation, Verdict
from supportagent.domain.model.flow import Flow

#: How many evidence lines per stage the model sees, and how long each may be.
MAX_LINES_PER_STAGE = 5
MAX_LINE_CHARS = 300


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    text: str


def facts(  # noqa: PLR0913, PLR0917 — the trace's parts, as the run holds them
    flow: Flow,
    object_id: str,
    observations: Sequence[StageObservation],
    verdict: Verdict,
    evidence: Mapping[str, Mapping[str, object]],
    causes: Sequence[str] = (),
) -> list[Fact]:
    titles = {s.id: s for s in flow.stages}
    out: list[Fact] = []
    for i, o in enumerate(observations, 1):
        stage = titles[o.stage]
        where = f"stage {i} '{stage.title}' ({stage.component})"
        if o.state == "found":
            when = f" at {o.at.isoformat()}" if o.at else ""
            text = f"{where} happened for {flow.object} {object_id}{when}"
            if o.rejected:
                text += ", and rejected it"
        elif o.state == "missing":
            text = f"{where} has no record of {flow.object} {object_id}"
        else:
            text = f"{where} couldn't be looked up: {o.reason}"
        out.append(Fact(f"S{i}", text))
    out.append(Fact("V", verdict_text(flow, object_id, verdict)))
    n = 0
    for o in observations:
        data = evidence.get(o.stage)
        if not data:
            continue
        for line in _lines(data, titles[o.stage].lookup.fields)[:MAX_LINES_PER_STAGE]:
            n += 1
            out.append(Fact(f"E{n}", f"{titles[o.stage].title}: {line[:MAX_LINE_CHARS]}"))
    out.extend(Fact(f"C{i}", c) for i, c in enumerate(causes, 1))
    return out


def _lines(data: Mapping[str, object], fields: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for path in fields:
        for value in values_at(data, path):
            if isinstance(value, str | int | bool) and str(value).strip():
                lines.append(f"{path.replace('[]', '')}={value}")
    return lines


def verdict_text(flow: Flow, object_id: str, v: Verdict) -> str:
    title = {s.id: s.title for s in flow.stages}
    obj = f"{flow.object} {object_id}"
    stage = title.get(v.stage or "", v.stage or "")
    since = f" since {v.since.isoformat()}" if v.since else ""
    text = {
        "complete": f"{obj} completed {flow.title}",
        "rejected": f"{obj} was rejected at '{stage}', as the flow allows",
        "in_progress": f"{obj} is waiting at '{stage}'{since}, within its expected time",
        "stuck": f"{obj} is stuck before '{stage}'{since}, past its expected time",
        "not_found": f"{obj} has no record at the first stage, '{stage}'",
        "cannot_tell": f"where {obj} is can't be told: '{stage}' couldn't be looked up",
    }[v.kind]
    if v.kind == "in_progress" and v.reason:
        text = f"{obj} is waiting at '{stage}'{since}; the flow sets no time limit for it"
    if v.late:
        text += "; late at " + ", ".join(f"'{title.get(s, s)}'" for s in v.late)
    return text


def template_answer(flow: Flow, object_id: str, v: Verdict) -> str:
    """The answer without a model: the verdict, in one sentence."""
    text = verdict_text(flow, object_id, v)
    return text[0].upper() + text[1:] + "."


def valid_cites(cites: Sequence[str], known: Sequence[Fact]) -> bool:
    ids = {f.id for f in known}
    return bool(cites) and all(c in ids for c in cites)


__all__ = ["Fact", "facts", "template_answer", "valid_cites", "verdict_text"]
