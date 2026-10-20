"""TriageService (10-wp WP-011): one UoW for dedup + decision, gate proposals after.

``TriageService.handle`` runs the ARCH §4 triage for one inbound alert:

1. Ingest and dedup the alert into a case (one unit of work).
2. Pre-classify, decide (via the injected classifier/LLM), and record the
   decision on the case (same unit of work).
3. If the decision is routed, submit the proposals through the ``GateService``
   (the same UoW instance, sequential transactions).

A safe default still writes the case, the decision and the events, and proposes
nothing. Every stage emits its event (``ingest``, ``dedup``, ``preclass``,
``redact``, ``llm``, ``decision``, ``gate``) so a replay shows the full path
(10-wp WP-011).

It is application code: it drives a structural UoW protocol (01 §1 layout,
importlinter — ``app`` may not import ``supportagent.adapters``) and speaks to
the world through ``ports`` and pure ``domain``.
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol, Self

from ulid import ULID

from supportagent.app.propose import build_action_specs
from supportagent.domain.events import EventKind
from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.event import Event
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.model.triage import (
    Linked,
    Retriaged,
    SafeDefaulted,
    Triaged,
    TriageDecision,
    TriageOutcome,
)
from supportagent.domain.model.upsert import UpsertResult
from supportagent.domain.routing import RoutingTable
from supportagent.domain.severity import severity_escalated
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator

#: Root span of a triage trace (the app mints trace/span ids; 01 §4).
_ROOT_SPAN = "span_0001"


def _span(stage: str) -> str:
    """A child span id for one triage stage."""
    return f"span_{stage}"


class _UoW(Protocol):
    """The slice of the SQL unit of work ``TriageService`` drives (01 §3).

    ``app`` may not import ``supportagent.adapters`` (importlinter), so the
    concrete ``SqlUnitOfWork`` is duck-typed through this structural protocol:
    an active context manager with an explicit ``commit`` (commit is explicit,
    never implicit — 10-wp).
    """

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _CaseRepo(Protocol):
    """The case repository ``TriageService`` reads and updates (01 §7)."""

    def get(self, case_id: str) -> dict[str, object] | None: ...

    def upsert_by_fingerprint(  # noqa: PLR0913 — mirrors the 01 §7.3 repo signature
        self,
        *,
        case_id: str,
        tenant_id: str,
        fingerprint: str,
        origin: str,
        trace_id: str,
        now: datetime,
    ) -> UpsertResult: ...

    def update(  # noqa: PLR0913 — the decision field set is the WP-011 contract
        self,
        *,
        case_id: str,
        service: str | None,
        severity: str | None,
        classification: str | None,
        external_table: str | None,
        external_id: str | None,
        status: str,
        decision_json: str,
        trace_id: str,
        now: datetime,
    ) -> None: ...


class _Classifier(Protocol):
    """The classifier ``TriageService`` delegates the decision to (ARCH §4)."""

    routing: RoutingTable

    def decide(self, evt: AlertEvent, case: Case, trace: TraceContext) -> TriageDecision: ...


class _Gate(Protocol):
    """The gate ``TriageService`` submits proposals through (INV-01)."""

    def submit(self, case_id: str, spec: ActionSpec, trace: TraceContext) -> Proposal: ...


class TriageService:
    """Triage one inbound alert: dedup + decision, then gate the proposals.

    8 keyword-only ports/config: the UoW, the 4 standard ports (clock, ids,
    events, case_repo), and the decision/propose collaborators (classifier,
    gate, catalog).
    """

    def __init__(  # noqa: PLR0913 — the signature is the service's public contract
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        case_repo: _CaseRepo,
        classifier: _Classifier,
        gate: _Gate,
        catalog: Catalog,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._case_repo = case_repo
        self._classifier = classifier
        self._gate = gate
        self._catalog = catalog

    # --- handle -------------------------------------------------------------

    def handle(self, evt: AlertEvent) -> TriageOutcome:
        """Triage ``evt`` and return the outcome (new case, linked, or retried).

        One unit of work covers ingest + dedup + (preclass, decision, case
        update). A routed decision then submits its proposals through the gate
        (sequential transactions on the same UoW). A safe default proposes
        nothing; a duplicate either links or retriggers an escalation.
        """
        trace_id = f"tr_{ULID()}"
        root = TraceContext.new(evt.tenant_id, trace_id, _ROOT_SPAN)
        fp = fingerprint(evt)
        now = self._clock.now()

        outcome: Literal["new", "linked", "retriaged"]
        decision: TriageDecision | None = None
        service: str | None = None
        case_id: str
        # INV-05: an LLM failure is fail-closed — the safe-default path does not
        # persist the case row (the UoW rolls back on exit); it returns the
        # decision to the caller and proposes nothing.
        commit = True

        with self._uow:
            self._emit(
                root,
                EventKind.INGEST,
                _span("ingest"),
                {"v": 1, "origin": evt.origin, "source": evt.source},
            )
            upsert = self._case_repo.upsert_by_fingerprint(
                case_id=self._ids.new(IdPrefix.CASE),
                tenant_id=evt.tenant_id,
                fingerprint=fp,
                origin=evt.origin,
                trace_id=trace_id,
                now=now,
            )
            case_id = upsert.case_id
            self._emit(
                root,
                EventKind.DEDUP,
                _span("dedup"),
                {"v": 1, "fingerprint": fp, "first_seen": not upsert.created},
            )

            if upsert.created:
                outcome = "new"
            else:
                row = self._case_repo.get(case_id)
                escalated = row is not None and severity_escalated(evt, row)
                outcome = "retriaged" if escalated else "linked"

            if outcome in ("new", "retriaged"):
                service = self._resolve_service(evt)
                case = self._case_for(evt, fp, case_id, service)
                self._emit(
                    root,
                    EventKind.PRECLASS,
                    _span("preclass"),
                    {"v": 1, "service": service},
                )
                decision = self._classifier.decide(evt, case, root.child(_span("classify")))
                severity_value = decision.severity.value if decision.severity is not None else None
                self._emit(
                    root,
                    EventKind.DECISION,
                    _span("decision"),
                    {
                        "v": 1,
                        "outcome": decision.outcome,
                        "reason": decision.reason,
                        "classification": decision.classification,
                        "severity": severity_value,
                        "route_id": decision.route_id,
                    },
                )
                if decision.outcome == "safe_default" and decision.reason == "llm-failure":
                    # INV-05 fail-closed: no LLM result means no triaged case.
                    # Skip the case update and leave the UoW uncommitted so the
                    # row (and stage events) roll back; nothing is proposed.
                    commit = False
                else:
                    self._case_repo.update(
                        case_id=case_id,
                        service=service,
                        severity=evt.severity_hint.value if evt.severity_hint is not None else None,
                        classification=decision.classification,
                        external_table=evt.table,
                        external_id=evt.external_id,
                        status="triaged",
                        decision_json=decision.model_dump_json(),
                        trace_id=trace_id,
                        now=self._clock.now(),
                    )
            if commit:
                self._uow.commit()

        if outcome == "linked":
            return Linked(case_id=case_id)
        # A non-linked outcome always decided above. Bandit B101 forbids `assert`,
        # so a defensive raise stands in for the mypy narrowing.
        if decision is None:  # defensive: unreachable — see the branches above
            raise RuntimeError("triage: a non-linked outcome requires a decision")  # noqa: TRY003
        if outcome == "retriaged":
            return Retriaged(case_id=case_id, decision=decision)
        # outcome == "new": a safe default proposes nothing; a routed decision
        # submits its proposals through the gate (same UoW, sequential UoWs).
        if decision.outcome == "safe_default":
            return SafeDefaulted(case_id=case_id, decision=decision)
        # A routed decision always carries the LLM's classification and severity
        # (classify.py populates both); the guard is only for mypy's narrowing.
        if decision.classification is None or decision.severity is None:
            raise RuntimeError(  # noqa: TRY003 — defensive: a routed decision is fully populated
                "triage: a routed decision requires classification and severity"
            )
        route = self._classifier.routing.lookup(decision.classification, decision.severity)
        full_case = self._case_for(evt, fp, case_id, service, decision.classification)
        specs = build_action_specs(route, full_case, self._catalog)
        proposals = [self._gate.submit(case_id, spec, root.child(_span("gate"))) for spec in specs]
        return Triaged(case_id=case_id, decision=decision, proposals=proposals)

    # --- helpers ------------------------------------------------------------

    def _resolve_service(self, evt: AlertEvent) -> str | None:
        """Resolve the case's service from the catalog (None ⇒ safe default)."""
        return self._catalog.resolve_service(resource=evt.resource, cmdb_ci=evt.cmdb_ci)

    def _case_for(
        self,
        evt: AlertEvent,
        fp: str,
        case_id: str,
        service: str | None,
        classification: str | None = None,
    ) -> Case:
        """A ``Case`` value for the classifier / proposal builder (01 §7)."""
        return Case(
            id=case_id,
            tenant_id=evt.tenant_id,
            fingerprint=fp,
            origin=evt.origin,
            service=service,
            external_table=evt.table,
            external_id=evt.external_id,
            severity=evt.severity_hint,
            classification=classification,
        )

    def _emit(
        self,
        root: TraceContext,
        kind: EventKind,
        span_id: str,
        payload: dict[str, object],
    ) -> None:
        """Append one triage-stage event to the active trace (01 §4)."""
        self._events.append(
            Event(
                id=self._ids.new(IdPrefix.EVENT),
                tenant_id=root.tenant_id,
                trace_id=root.trace_id,
                span_id=span_id,
                parent_span_id=root.span_id,
                kind=kind,
                actor="agent",
                payload=payload,
                ts=self._clock.now(),
            )
        )


__all__ = ["TriageService"]
