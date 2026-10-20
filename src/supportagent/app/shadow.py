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
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from supportagent.app.triage import TriageCaseRepo, TriageClassifier, TriageService
from supportagent.domain.hashing import payload_hash
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.model.shadow import HistoricalIncident, ShadowRecord
from supportagent.domain.model.triage import Linked, Retriaged, TriageDecision
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.severity import Severity
from supportagent.domain.shadow import agreement
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator


class _UoW(Protocol):
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


__all__ = ["SettableClock", "ShadowRunner", "import_history"]
