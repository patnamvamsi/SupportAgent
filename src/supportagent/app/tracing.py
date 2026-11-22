"""FlowTracer (ADR-0021 §3-§5): one object, looked up stage by stage.

For each stage of a reviewed flow, the tracer fills the traced id (and keys
derived from earlier stages) into the lookup's literal arguments, calls the
capability through the ``ToolRegistry`` (bound, schema-checked, audited like
every tool call), and reads three things off the raw result: whether the
stage happened, when, and whether it rejected the object. The verdict comes
from those alone (``domain.flow_trace.judge``): the model never decides where
an object stopped.

What the model may later see is each stage's allowed fields, pseudonymised
(``PiiGuard``). If the PII analyser can't run, ``evidence`` is empty and
``pii_error`` says why: the timeline stands, and nothing unanalysed is passed
on.

A stage is ``unknown``, never ``missing``, when its lookup couldn't run: no
tool serves the capability, a key it needs wasn't found, a reference sits in
an argument the tool doesn't take literally, or the call failed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from supportagent.app.pii import PiiGuard
from supportagent.app.tools import ToolRegistry
from supportagent.domain.errors import DomainValidationError, PiiUnavailable, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.fieldpath import values_at
from supportagent.domain.flow_trace import StageObservation, Verdict, judge
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.catalog import CatalogView, ServiceEntry
from supportagent.domain.model.event import Event
from supportagent.domain.model.flow import REF, SAFE_VALUE, Flow, FlowStage
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.pii import PiiAnalyzer


class _Unlookable(Exception):  # noqa: N818 — internal control flow, never escapes
    pass


@dataclass(frozen=True, slots=True)
class TraceRun:
    flow: str
    object_id: str
    observations: tuple[StageObservation, ...]
    verdict: Verdict
    #: Per stage, the allowed fields pseudonymised; empty when PII couldn't be checked.
    evidence: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    #: Distinct values pseudonymised per entity (never the values).
    pii_counts: Mapping[str, int] = field(default_factory=dict)
    pii_error: str | None = None
    #: The question's pseudonymiser, for text derived later (RCA's claims), so the
    #: same value keeps its placeholder; ``None`` when PII couldn't be checked.
    pseudonymise: Callable[[str], str] | None = field(default=None, compare=False, repr=False)
    #: Placeholders back to values, for the person who asked (the trace page);
    #: never sent to the model or recorded. Identity when nothing was pseudonymised.
    reveal: Callable[[str], str] = field(default=lambda text: text, compare=False, repr=False)

    def as_dict(self) -> dict[str, object]:
        v = self.verdict
        return {
            "flow": self.flow,
            "object_id": self.object_id,
            "verdict": {
                "kind": v.kind,
                "stage": v.stage,
                "deviation": v.deviation,
                "since": v.since.isoformat() if v.since else None,
                "late": list(v.late),
                "uncorrelated": list(v.uncorrelated),
                "reason": v.reason,
            },
            "stages": [
                {
                    "stage": o.stage,
                    "state": o.state,
                    "at": o.at.isoformat() if o.at else None,
                    "rejected": o.rejected,
                    "reason": o.reason,
                }
                for o in self.observations
            ],
            "pii_counts": dict(self.pii_counts),
            "pii_error": self.pii_error,
        }


class FlowTracer:
    def __init__(  # noqa: PLR0913 — the registry, the catalog and the audit ports
        self,
        *,
        tools: ToolRegistry,
        catalog: CatalogView,
        pii: PiiAnalyzer | None,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._tools = tools
        self._services = {s.name: s for s in catalog.services}
        self._pii = pii
        self._events = events
        self._ids = ids
        self._clock = clock

    def trace(  # noqa: PLR0913 — the question, who asks, when, and drafts
        self,
        flow: Flow,
        object_id: str,
        trace: TraceContext,
        *,
        actor: str,
        at: datetime | None = None,
        allow_draft: bool = False,
    ) -> TraceRun:
        """Trace ``object_id`` through ``flow``, as of ``at`` (default: now).

        ``allow_draft`` is for ``flow verify`` only: a reviewer checks a draft's
        lookups before marking it reviewed.
        """
        if not flow.reviewed and not allow_draft:
            raise DomainValidationError(f"flow {flow.flow} is an unreviewed draft")  # noqa: TRY003
        if not flow.accepts(object_id):
            raise DomainValidationError(  # noqa: TRY003
                f"{object_id!r} isn't a {flow.object} id flow {flow.flow} accepts"
            )
        now = at or self._clock.now()
        keys: dict[str, str] = {"object.id": object_id}
        raw: dict[str, Mapping[str, object]] = {}
        observations: list[StageObservation] = []
        for stage in flow.stages:
            try:
                data = self._lookup(stage, keys, trace, at)
            except _Unlookable as exc:
                observations.append(StageObservation(stage.id, "unknown", reason=str(exc)))
                continue
            raw[stage.id] = data
            observations.append(_observe(stage, data))
            for key in flow.keys:
                if key.stage == stage.id:
                    value = next(
                        (str(v) for v in values_at(data, key.field) if SAFE_VALUE.match(str(v))),
                        None,
                    )
                    if value is not None:
                        keys[f"key.{key.name}"] = value
        verdict = judge(flow, {o.stage: o for o in observations}, now=now)
        evidence, counts, error, guard = self._guard(flow, object_id, raw)
        run = TraceRun(
            flow.flow,
            object_id,
            tuple(observations),
            verdict,
            evidence,
            counts,
            error,
            guard.text if guard is not None else None,
            guard.pseudonyms.reveal if guard is not None else (lambda text: text),
        )
        self._audit(run, trace, actor=actor, read=sorted(raw), records=flow.reads_records())
        return run

    def _audit(
        self, run: TraceRun, trace: TraceContext, *, actor: str, read: list[str], records: bool
    ) -> None:
        """One ``trace`` event: who asked, what was read, the verdict; never a PII value."""
        event_id = self._ids.new(IdPrefix.EVENT)
        v = run.verdict
        self._events.append(
            Event(
                id=event_id,
                tenant_id=trace.tenant_id,
                trace_id=trace.trace_id,
                span_id=f"trace_{event_id}",
                parent_span_id=trace.span_id,
                kind=EventKind.TRACE,
                actor=actor,
                status="ok",
                payload={
                    "v": 1,
                    "flow": run.flow,
                    "object_id": run.object_id,
                    "stages_read": read,
                    "reads_records": records,
                    "verdict": v.kind,
                    "stage": v.stage,
                    "deviation": v.deviation,
                    "pii_counts": dict(run.pii_counts),
                    "pii_error": run.pii_error,
                },
                ts=self._clock.now(),
            )
        )

    def _lookup(
        self, stage: FlowStage, keys: Mapping[str, str], trace: TraceContext, at: datetime | None
    ) -> Mapping[str, object]:
        lookup = stage.lookup
        tool = self._tools.get(lookup.capability)
        if tool is None:
            raise _Unlookable(f"no tool serves {lookup.capability}")  # noqa: TRY003
        args: dict[str, object] = {}
        for name, value in lookup.args.items():
            if isinstance(value, str) and REF.search(value):
                if name not in tool.literal_args:
                    raise _Unlookable(f"{lookup.capability} doesn't take {name!r} literally")  # noqa: TRY003
                args[name] = _fill(value, keys)
            else:
                args[name] = value
        service = self._service(stage.component)
        try:
            if at is not None and self._tools.replayable(lookup.capability):
                result = self._tools.call_as_of(lookup.capability, args, service, trace, at=at)
            else:
                result = self._tools.call(lookup.capability, args, service, trace)
        except (ToolError, DomainValidationError) as exc:
            raise _Unlookable(f"{lookup.capability} failed: {str(exc)[:160]}") from exc  # noqa: TRY003
        return result.data

    def _service(self, component: str) -> ServiceEntry:
        name = component.split(":", 1)[-1]
        found = self._services.get(name)
        if found is not None:
            return found
        try:
            return ServiceEntry(name=name)
        except DomainValidationError as exc:
            raise _Unlookable(f"component {component!r} isn't a catalog service") from exc  # noqa: TRY003

    def _guard(
        self, flow: Flow, object_id: str, raw: Mapping[str, Mapping[str, object]]
    ) -> tuple[dict[str, Mapping[str, object]], dict[str, int], str | None, PiiGuard | None]:
        if self._pii is None:
            return {}, {}, "no PII analyser is configured (pii.engine: off)", None
        guard = PiiGuard(self._pii, keep=[object_id])
        fields = {s.id: s.lookup.fields for s in flow.stages}
        try:
            evidence: dict[str, Mapping[str, object]] = {
                sid: guard.clean(data, fields[sid]) for sid, data in raw.items()
            }
        except PiiUnavailable as exc:
            return {}, {}, str(exc), None
        return evidence, guard.pseudonyms.counts, None, guard


def _fill(template: str, keys: Mapping[str, str]) -> str:
    def one(match: object) -> str:
        ref = match.group(1)  # type: ignore[attr-defined]
        value = keys.get(ref)
        if value is None:
            raise _Unlookable(f"{ref} wasn't found in an earlier stage")  # noqa: TRY003
        if not SAFE_VALUE.match(value):
            raise _Unlookable(f"{ref} isn't a value a query may carry")  # noqa: TRY003
        return value

    return REF.sub(one, template)


def _observe(stage: FlowStage, data: Mapping[str, object]) -> StageObservation:
    lookup = stage.lookup
    found = any(v not in (None, "", [], {}) for v in values_at(data, lookup.found))
    if not found:
        return StageObservation(stage.id, "missing")
    times = (
        sorted(t for t in (_time(v) for v in values_at(data, lookup.at)) if t) if lookup.at else []
    )
    rejected = stage.rejection is not None and any(
        stage.rejection.matches(v) for v in values_at(data, stage.rejection.path)
    )
    return StageObservation(stage.id, "found", times[0] if times else None, rejected=rejected)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["FlowTracer", "TraceRun"]
