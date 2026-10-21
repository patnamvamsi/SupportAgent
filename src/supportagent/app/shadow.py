"""ShadowRunner and shadow import (ARCH §8.4, CAP §4.7, 10-wp WP-014).

``ShadowRunner.run(evt)`` is Loop A with the hands removed. It drives the very
same ``TriageService`` (ingest, dedup, preclass, classify, decision,
``build_action_specs``) but hands it a *recording gate*: where live mode would
submit a proposal, shadow notes the ActionSpec and the moment it would have
been proposed. Nothing reaches ``proposals``, ``approvals`` or ``outbox``, and
no ``gate`` event is written. The case row is still written, so INV-02 dedup
holds in shadow exactly as live.

Each new case gets one ``shadow_records`` row in the same unit of work as the
triage (``TriageService``'s ``with`` block joins this one). A duplicate or a
re-triage returns the case's existing record: the counterfactual
"first proposal" is the first one.

``import_history`` replays a partner export through the runner in
``opened_at`` order, with the clock set to each incident's ``opened_at``, then
records the human's final labels and the agreement.

``ShadowInbound`` is the ``InboundHandler`` the shadow worker binds to
``SyncService`` (02 §7 ``sync.poll.<table>``): a new record goes through the
runner; changes to a known record record a ``sync`` event and go through
``LifecycleService`` (WP-101: takeover, resolve, reopen, hand-back).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.app.triage import (
    TriageCaseRepo,
    TriageClassifier,
    TriageGrouping,
    TriageMemory,
    TriageService,
    TriageSpend,
)
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import payload_hash
from supportagent.domain.ids import IdPrefix
from supportagent.domain.lifecycle import CaseEvent
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.event import Event
from supportagent.domain.model.external import ExternalCaseRef
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.model.shadow import HistoricalIncident, ShadowRecord
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.domain.model.triage import Linked, Retriaged, TriageDecision
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.severity import Severity
from supportagent.domain.shadow import agreement
from supportagent.domain.takeover import LifecycleSignal
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.paging import PagingIntegration


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _ShadowRepo(Protocol):
    def insert(self, record: ShadowRecord) -> None: ...

    def by_case(self, tenant_id: str, case_id: str) -> ShadowRecord | None: ...

    def set_human(
        self,
        record_id: str,
        *,
        classification: str | None,
        severity: Severity | None,
        first_action_at: datetime | None,
        agreement: bool | None,
    ) -> None: ...


class _CaseLookup(Protocol):
    def by_external(
        self, tenant_id: str, table: str, external_id: str
    ) -> Mapping[str, object] | None: ...


class _Lifecycle(Protocol):
    def apply(self, case_id: str, changes: Sequence[ChangeRecord]) -> Sequence[LifecycleSignal]: ...


class _MemoryIndex(Protocol):
    def index_resolved(self, case_id: str) -> None: ...


class _Feedback(Protocol):
    def record_changes(self, case_id: str, changes: Sequence[ChangeRecord]) -> int: ...

    def record_outcome(self, case_id: str, *, resolved_by: str) -> None: ...


class SettableClock(Clock, Protocol):
    def set(self, when: datetime) -> None: ...


class _RecordingGate:
    """Stands in for ``GateService`` in shadow: notes what would be submitted.

    Returns an unpersisted ``proposed`` Proposal so ``TriageService`` runs its
    unchanged code path; nothing is written and no gate event is emitted.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self.submitted: list[tuple[ActionSpec, datetime]] = []

    def submit(self, case_id: str, spec: ActionSpec, trace: TraceContext) -> Proposal:
        now = self._clock.now()
        self.submitted.append((spec, now))
        return Proposal(
            id=f"shadow:{case_id}:{len(self.submitted)}",
            tenant_id=trace.tenant_id,
            case_id=case_id,
            action_kind=spec.kind,
            scope=spec.scope,
            payload=spec.payload,
            payload_hash=payload_hash(spec.payload),
            trace_id=trace.trace_id,
            status=ProposalStatus.PROPOSED,
            created_by="agent",
            created_at=now,
        )


def _spec_json(spec: ActionSpec) -> dict[str, object]:
    return {
        "kind": spec.kind.value,
        "target": spec.target.model_dump(mode="json"),
        "scope": spec.scope,
        "payload": dict(spec.payload),
    }


class ShadowRunner:
    """Loop A without side effects; one counterfactual record per case."""

    def __init__(  # noqa: PLR0913 — TriageService's ports plus the shadow repo
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        case_repo: TriageCaseRepo,
        classifier: TriageClassifier,
        catalog: Catalog,
        repo: _ShadowRepo,
        tenant_constants: Sequence[str] = (),
        grouping: TriageGrouping | None = None,
        spend: TriageSpend | None = None,
        memory: TriageMemory | None = None,
        paging: PagingIntegration | None = None,
    ) -> None:
        self._uow = uow
        self._ids = ids
        self._repo = repo
        self._gate = _RecordingGate(clock)
        self._triage = TriageService(
            uow=uow,
            clock=clock,
            ids=ids,
            events=events,
            case_repo=case_repo,
            classifier=classifier,
            gate=self._gate,
            catalog=catalog,
            tenant_constants=tenant_constants,
            grouping=grouping,
            spend=spend,
            memory=memory,
            paging=paging,
        )

    def run(self, evt: AlertEvent) -> ShadowRecord | None:
        """Triage ``evt`` in shadow; ``None`` only for a duplicate of a non-shadow case."""
        with self._uow:
            self._gate.submitted.clear()
            outcome = self._triage.handle(evt)
            existing = self._repo.by_case(evt.tenant_id, outcome.case_id)
            if existing is not None or isinstance(outcome, (Linked, Retriaged)):
                self._uow.commit()
                return existing
            submitted = list(self._gate.submitted)
            record = ShadowRecord(
                id=self._ids.new(IdPrefix.SHADOW),
                tenant_id=evt.tenant_id,
                case_id=outcome.case_id,
                alert_received_at=evt.received_at,
                first_proposal_at=submitted[0][1] if submitted else None,
                proposed={
                    "decision": outcome.decision.model_dump(mode="json"),
                    "specs": [_spec_json(spec) for spec, _ in submitted],
                    "external_id": evt.external_id,
                    "ticket_number": evt.ticket_number,
                },
            )
            self._repo.insert(record)
            self._uow.commit()
        return record

    def label(self, record: ShadowRecord, incident: HistoricalIncident) -> ShadowRecord:
        """Record the human's final labels and the agreement (CAP §4.7)."""
        # Stored as JSON: validate in JSON mode, where enums parse from strings.
        decision = TriageDecision.model_validate_json(json.dumps(record.proposed["decision"]))
        agreed = agreement(
            decision.classification,
            decision.severity,
            incident.human_classification,
            incident.human_severity,
        )
        with self._uow:
            self._repo.set_human(
                record.id,
                classification=incident.human_classification,
                severity=incident.human_severity,
                first_action_at=incident.human_first_action_at,
                agreement=agreed,
            )
            self._uow.commit()
        return record.model_copy(
            update={
                "human_classification": incident.human_classification,
                "human_severity": incident.human_severity,
                "human_first_action_at": incident.human_first_action_at,
                "agreement": agreed,
            }
        )


def import_history(
    incidents: Iterable[HistoricalIncident], *, runner: ShadowRunner, clock: SettableClock
) -> list[ShadowRecord]:
    """Replay incidents in ``opened_at`` order with the clock at each ``opened_at``."""
    ordered = sorted(incidents, key=lambda i: (i.opened_at, i.evt.external_id))
    records: list[ShadowRecord] = []
    for incident in ordered:
        clock.set(incident.opened_at)
        record = runner.run(incident.evt)
        if record is not None:
            records.append(runner.label(record, incident))
    return records


class _Runner(Protocol):
    """``ShadowRunner`` in shadow mode; ``TriageRunner`` in assisted mode (ADR-0008)."""

    def run(self, evt: AlertEvent) -> object: ...


class ShadowInbound:
    """The shadow worker's ``InboundHandler`` (02 §6-§7).

    ``on_alert`` runs the record through ``ShadowRunner`` — nothing is
    proposed or sent. ``on_changes`` records one ``sync`` event on the case's
    trace (a fresh trace when the record has no case, e.g. it predates the
    sync cursor). The payload names the changed fields and feeds, never their
    values: the values stay in ServiceNow.
    """

    def __init__(  # noqa: PLR0913 — the runner plus the event-write ports
        self,
        *,
        runner: _Runner,
        uow: _UoW,
        cases: _CaseLookup,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
        tenant_id: str,
        lifecycle: _Lifecycle | None = None,
        feedback: _Feedback | None = None,
        memory: _MemoryIndex | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._memory = memory
        self._feedback = feedback
        self._runner = runner
        self._uow = uow
        self._cases = cases
        self._events = events
        self._ids = ids
        self._clock = clock
        self._tenant = tenant_id

    def on_alert(self, evt: AlertEvent) -> None:
        self._runner.run(evt)

    def on_changes(self, case_ref: ExternalCaseRef, changes: Sequence[ChangeRecord]) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        with self._uow:
            case = self._cases.by_external(self._tenant, case_ref.table, case_ref.sys_id)
            trace_id = str(case["trace_id"]) if case is not None else f"tr_{event_id}"
            self._events.append(
                Event(
                    id=event_id,
                    tenant_id=self._tenant,
                    trace_id=trace_id,
                    span_id=f"sync_{event_id}",
                    parent_span_id=None,
                    kind=EventKind.SYNC,
                    actor="agent",
                    payload={
                        "v": 1,
                        "table": case_ref.table,
                        "sys_id": case_ref.sys_id,
                        "number": case_ref.number,
                        "case_id": None if case is None else str(case["id"]),
                        "rows": len(changes),
                        "fields": sorted({c.fieldname for c in changes if c.fieldname}),
                        "sources": sorted({c.source for c in changes}),
                    },
                    ts=self._clock.now(),
                )
            )
            if case is not None and self._feedback is not None:
                # WP-109: the ``diff`` moment, each human change vs what we proposed.
                self._feedback.record_changes(str(case["id"]), changes)
            if case is not None and self._lifecycle is not None:
                # WP-101: takeover / resolve / reopen / hand-back, in this same UoW.
                signals = self._lifecycle.apply(str(case["id"]), changes)
                resolved = [s for s in signals if s.event is CaseEvent.RESOLVED]
                if resolved and self._feedback is not None:
                    # WP-109: the ``outcome`` moment; fills the shadow record's labels.
                    self._feedback.record_outcome(
                        str(case["id"]), resolved_by=resolved[-1].change.user
                    )
                if resolved and self._memory is not None:
                    # WP-108: a resolved case becomes a retrievable past case.
                    self._memory.index_resolved(str(case["id"]))
            self._uow.commit()


__all__ = ["SettableClock", "ShadowInbound", "ShadowRunner", "import_history"]
