"""TriageService (10-wp WP-011): one UoW for dedup + decision, gate proposals after.

``TriageService.handle`` runs the ARCH §4 triage for one inbound alert:

1. Ingest and dedup the alert into a case (one unit of work).
2. Pre-classify, decide (via the injected classifier/LLM), and record the
   decision on the case (same unit of work).
3. If the decision is routed, submit the proposals through the ``GateService``
   inside the same unit of work (its ``with`` block joins the outer one).

A safe default still writes the case, the decision and the events, and proposes
nothing. Every stage emits its event (``ingest``, ``dedup``, ``preclass``,
``redact``, ``llm``, ``decision``, ``gate``) so a replay shows the full path
(10-wp WP-011).

It is application code: it drives a structural UoW protocol (01 §1 layout,
importlinter — ``app`` may not import ``supportagent.adapters``) and speaks to
the world through ``ports`` and pure ``domain``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa
from ulid import ULID

from supportagent.app.classify import safe_default
from supportagent.app.grouping import GroupOutcome
from supportagent.app.propose import build_action_specs, page_recommended_spec
from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.events import EventKind
from supportagent.domain.fingerprint import fingerprint
from supportagent.domain.ids import IdPrefix
from supportagent.domain.kinds import ActionKind
from supportagent.domain.lifecycle import CaseEvent, CaseStatus, next_status
from supportagent.domain.memory import Citation
from supportagent.domain.model.action import ActionSpec
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.event import Event
from supportagent.domain.model.proposal import Proposal, Skipped
from supportagent.domain.model.triage import (
    Linked,
    Retriaged,
    SafeDefaulted,
    Triaged,
    TriageDecision,
    TriageOutcome,
    TriageResult,
)
from supportagent.domain.model.upsert import UpsertResult
from supportagent.domain.proposal_state import ProposalEvent, ProposalStatus
from supportagent.domain.proposal_state import next_status as next_proposal_status
from supportagent.domain.routing import RoutingTable
from supportagent.domain.severity import Severity, severity_escalated
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.paging import PagingIntegration

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


class TriageCaseRepo(Protocol):
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


class TriageClassifier(Protocol):
    """The classifier ``TriageService`` delegates the decision to (ARCH §4)."""

    routing: RoutingTable

    def decide(self, evt: AlertEvent, case: Case, trace: TraceContext) -> TriageDecision: ...


class _Gate(Protocol):
    """The gate ``TriageService`` submits proposals through (INV-01)."""

    def submit(self, case_id: str, spec: ActionSpec, trace: TraceContext) -> Proposal | Skipped: ...


class TriageGrouping(Protocol):
    """``GroupingService.assign`` (WP-111)."""

    def assign(
        self, case_id: str, *, service: str | None, source: str, now: datetime
    ) -> GroupOutcome: ...


class TriageSpend(Protocol):
    """``SpendBreaker`` (WP-111)."""

    budget: int

    def allow(self, tenant_id: str, now: datetime) -> bool: ...


def _upstream(evt: AlertEvent) -> str:
    """The upstream source for grouping rule (c) (01 §7.8): adapter plus alert type.

    The adapter alone (``servicenow``) is shared by every case, which would
    make rule (c) group a whole storm. For ServiceNow the alert type is
    ``u_alert_source``, the monitoring tool behind the record.
    """
    return f"{evt.source}:{evt.alert_type or evt.origin}"


def _storm_member_decision(
    lead_case_id: str | None, lead: Mapping[str, object] | None
) -> TriageDecision:
    """A storm member's safe default: the lead's labels as the suggestion."""
    classification = lead.get("classification") if lead else None
    severity = lead.get("severity") if lead else None
    suggestion = None
    if isinstance(classification, str) and isinstance(severity, str):
        suggestion = TriageResult(
            classification=classification,
            severity=Severity(severity),
            summary=f"storm group member; lead case {lead_case_id}",
            confidence=0.0,
        )
    return safe_default(
        "storm-member",
        classification=suggestion.classification if suggestion else None,
        severity=suggestion.severity if suggestion else None,
        llm_suggestion=suggestion,
    )


class TriageMemory(Protocol):
    """``CaseMemory`` (WP-108)."""

    def remember(
        self,
        case_id: str,
        evt: AlertEvent,
        *,
        service: str | None,
        classification: str | None,
        severity: str | None,
    ) -> None: ...

    def similar(self, case_id: str, *, k: int = 3) -> Sequence[Citation]: ...


class _Planner(Protocol):
    """``CommsPlanner``: a route's comms template → a plan proposal (WP-102)."""

    def plan(self, case_id: str, plan_template: str, trace: TraceContext) -> Proposal | Skipped: ...


class TriageRunner:
    """Assisted mode's runner for ``ShadowInbound`` (ADR-0008): the real triage.

    Shadow wires ``ShadowRunner`` (Loop A without hands); assisted wires this,
    so proposals go through the gate and grant-covered ones are enqueued.
    """

    def __init__(self, triage: TriageService) -> None:
        self._triage = triage

    def run(self, evt: AlertEvent) -> TriageOutcome:
        return self._triage.handle(evt)


class TriageService:
    """Triage one inbound alert: dedup + decision, then gate the proposals.

    Keyword-only ports/config: the UoW, the 4 standard ports (clock, ids,
    events, case_repo), the decision/propose collaborators (classifier,
    gate, catalog), and the tenant constants for the fingerprint.
    """

    def __init__(  # noqa: PLR0913 — the signature is the service's public contract
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        case_repo: TriageCaseRepo,
        classifier: TriageClassifier,
        gate: _Gate,
        catalog: Catalog,
        tenant_constants: Sequence[str] = (),
        planner: _Planner | None = None,
        grouping: TriageGrouping | None = None,
        spend: TriageSpend | None = None,
        memory: TriageMemory | None = None,
        paging: PagingIntegration | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._case_repo = case_repo
        self._classifier = classifier
        self._gate = gate
        self._planner = planner
        self._grouping = grouping
        self._spend = spend
        self._memory = memory
        self._paging = paging
        self._catalog = catalog
        self._unpaged = frozenset(catalog.unpaged_services())
        #: ``redaction.tenant_constants`` (02 §1): folded to ``<const>`` in the
        #: fingerprint (01 §7.2 step 5) so per-tenant literals don't split cases.
        self._tenant_constants = tuple(tenant_constants)

    # --- handle -------------------------------------------------------------

    def handle(self, evt: AlertEvent) -> TriageOutcome:
        """Triage ``evt`` and return the outcome (new case, linked, or re-triaged).

        One unit of work covers ingest, dedup, preclass, the decision, the case
        update and — for a routed decision — the gate proposals (10-wp WP-011),
        so a routed case never commits without its proposals. A safe default
        (including ``llm-failure``) still writes the case, the decision and the
        events, and proposes nothing (INV-05: the human queue). A duplicate
        either links or, on a severity escalation, re-triages in place.
        """
        trace_id = f"tr_{ULID()}"
        root = TraceContext.new(evt.tenant_id, trace_id, _ROOT_SPAN)
        fp = fingerprint(evt, self._tenant_constants)
        now = self._clock.now()

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
                {"v": 1, "fingerprint": fp, "first_seen": upsert.created},
            )

            # A re-triage keeps the case's lifecycle status: severity escalation
            # re-decides in place and never takes a case back from a human
            # (01 §7.11 has no re-triage transition).
            status: str
            if upsert.created:
                status = CaseStatus.OPEN.value
            else:
                row = self._case_repo.get(case_id)
                if row is None or not severity_escalated(evt, row):
                    self._uow.commit()
                    return Linked(case_id=case_id)
                status = str(row["status"])

            service = self._resolve_service(evt)
            self._emit(root, EventKind.PRECLASS, _span("preclass"), {"v": 1, "service": service})
            decision = self._decide(evt, fp, case_id, service, root, created=upsert.created)
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
            if upsert.created:
                event = (
                    CaseEvent.TRIAGED if decision.outcome == "routed" else CaseEvent.SAFE_DEFAULTED
                )
                status = next_status(CaseStatus.OPEN, event).value
            self._case_repo.update(
                case_id=case_id,
                service=service,
                severity=evt.severity_hint.value if evt.severity_hint is not None else None,
                classification=decision.classification,
                external_table=evt.table,
                external_id=evt.external_id,
                status=status,
                decision_json=decision.model_dump_json(),
                trace_id=trace_id,
                now=self._clock.now(),
            )
            if self._memory is not None and upsert.created:
                # WP-108: every new case is remembered; it's retrievable once resolved.
                self._memory.remember(
                    case_id,
                    evt,
                    service=service,
                    classification=decision.classification,
                    severity=decision.severity.value if decision.severity else None,
                )

            result: TriageOutcome
            if not upsert.created:
                result = self._retriage(evt, fp, case_id, service, decision, status, root)
            elif decision.outcome == "safe_default":
                result = SafeDefaulted(case_id=case_id, decision=decision)
            else:
                proposals = self._propose(evt, fp, case_id, service, decision, root)
                result = Triaged(case_id=case_id, decision=decision, proposals=proposals)
            self._uow.commit()
        return result

    def _decide(  # noqa: PLR0913 — the case coordinates each branch needs
        self,
        evt: AlertEvent,
        fp: str,
        case_id: str,
        service: str | None,
        root: TraceContext,
        *,
        created: bool,
    ) -> TriageDecision:
        """Grouping, then the first branch that applies: paging, storm member, spend, classifier."""
        now = self._clock.now()
        group = (
            self._grouping.assign(case_id, service=service, source=_upstream(evt), now=now)
            if self._grouping is not None and created
            else None
        )
        if group is not None and group.group_id is not None:
            self._emit(
                root,
                EventKind.DEDUP,
                _span("group"),
                {
                    "v": 1,
                    "group_id": group.group_id,
                    "action": group.decision.action,
                    "rule": group.decision.rule,
                    "storm": group.decision.storm,
                },
            )
        if service is not None and service in self._unpaged:
            # ARCH §14 / ADR-0006: paging stays with the customer's own
            # tooling, so a service without it is never triaged.
            decision = safe_default("no-paging-integration")
        elif group is not None and group.decision.storm_member:
            # ARCH §4.3 / ADR-0007: one triage per storm group; members
            # inherit the lead's labels as a suggestion, for a human.
            decision = _storm_member_decision(group.lead_case_id, group.lead_decision)
        elif (
            service is not None
            and self._spend is not None
            and not self._spend.allow(evt.tenant_id, now)
        ):
            # ARCH §11: over the daily budget, safe-default without the LLM.
            self._emit(
                root,
                EventKind.SPEND_BREAKER,
                _span("spend"),
                {"v": 1, "budget_tokens": self._spend.budget},
            )
            decision = safe_default("spend-breaker")
        else:
            decision = self._classifier.decide(
                evt, self._case_for(evt, fp, case_id, service), root.child(_span("classify"))
            )
        return decision

    def _propose(  # noqa: PLR0913, PLR0917 — the case coordinates the proposal builder needs
        self,
        evt: AlertEvent,
        fp: str,
        case_id: str,
        service: str | None,
        decision: TriageDecision,
        root: TraceContext,
    ) -> list[Proposal]:
        """Submit the routed decision's proposals inside the caller's UoW (INV-01)."""
        # A routed decision always carries the LLM's classification and severity
        # (classify.py populates both); the guard is only for mypy's narrowing.
        if decision.classification is None or decision.severity is None:
            raise RuntimeError(  # noqa: TRY003 — defensive: a routed decision is fully populated
                "triage: a routed decision requires classification and severity"
            )
        route = self._classifier.routing.lookup(decision.classification, decision.severity)
        full_case = self._case_for(evt, fp, case_id, service, decision.classification)
        specs = self._with_citation(case_id, build_action_specs(route, full_case, self._catalog))
        recommended = self._page_recommendation(
            full_case.model_copy(update={"severity": decision.severity}), root
        )
        if recommended is not None:
            specs.append(recommended)
        submitted = [self._gate.submit(case_id, spec, root.child(_span("gate"))) for spec in specs]
        if route.comms and self._planner is not None:
            # WP-102: the route's comms plan is a proposal of its own, in this UoW.
            submitted.append(self._planner.plan(case_id, route.comms, root.child(_span("comms"))))
        return [p for p in submitted if isinstance(p, Proposal)]

    def _retriage(  # noqa: PLR0913, PLR0917 — the case coordinates the proposal builder needs
        self,
        evt: AlertEvent,
        fp: str,
        case_id: str,
        service: str | None,
        decision: TriageDecision,
        status: str,
        root: TraceContext,
    ) -> Retriaged:
        """ARCH §4.1 / BL-019: escalation re-runs classification **and routing**.

        The case's open proposals were built for the old severity, so they're
        superseded (a ``gate`` event each) before a routed re-decision submits
        its own. A human-owned case is left alone: it gets no proposals, and a
        person's pending decisions aren't withdrawn under them.
        """
        if status == CaseStatus.HUMAN_OWNED.value:
            return Retriaged(case_id=case_id, decision=decision)
        superseded = self._supersede_open(case_id, root)
        proposals = (
            self._propose(evt, fp, case_id, service, decision, root)
            if decision.outcome == "routed"
            else []
        )
        return Retriaged(
            case_id=case_id, decision=decision, proposals=proposals, superseded=superseded
        )

    def _supersede_open(self, case_id: str, root: TraceContext) -> list[str]:
        conn = self._uow.connection
        rows = conn.execute(
            sa.text(
                "SELECT id, status FROM proposals WHERE case_id = :c "
                "AND status IN ('pending_approval', 'approved')"
            ),
            {"c": case_id},
        ).all()
        out: list[str] = []
        for proposal_id, current in rows:
            to = next_proposal_status(ProposalStatus(current), ProposalEvent.CASE_RETRIAGED)
            conn.execute(
                sa.text("UPDATE proposals SET status = :to WHERE id = :id AND status = :from"),
                {"to": to.value, "id": proposal_id, "from": current},
            )
            self._emit(
                root,
                EventKind.GATE,
                _span("gate"),
                {
                    "v": 1,
                    "transition": "superseded",
                    "proposal_id": str(proposal_id),
                    "reason": "case_retriaged",
                },
            )
            out.append(str(proposal_id))
        return out

    def _page_recommendation(self, case: Case, root: TraceContext) -> ActionSpec | None:
        """ARCH §14: a P1/P2 with no open page gets ``page_recommended``, never a page.

        The answer comes from the service's real paging provider. Without one
        (the Null Object), nothing is looked up or recommended and ``doctor``
        warns. A failed lookup recommends: a person should check.
        """
        if self._paging is None or case.severity not in (Severity.P1, Severity.P2):
            return None
        entry = next((e for e in self._catalog.services if e.name == case.service), None)
        integration = entry.paging_integration if entry is not None else None
        if not integration or not self._paging.supports(integration):
            return None
        why = "no open page for this alert"
        try:
            page = self._paging.find_open_page(integration, case.external_id or "")
        except ExternalServiceError as exc:
            page, why = None, f"page lookup failed: {type(exc).__name__}"
        if page is not None:
            return None
        self._emit(
            root,
            EventKind.PAGE_RECOMMENDED,
            _span("page"),
            {
                "v": 1,
                "severity": case.severity.value,
                "integration": integration,
                "why": why,
            },
        )
        return page_recommended_spec(case, integration, why)

    def _with_citation(self, case_id: str, specs: list[ActionSpec]) -> list[ActionSpec]:
        """A work note cites the most similar resolved case, when one exists (WP-108).

        The citation is an id the gate checks exists. With no match nothing
        is cited (ARCH §5.2 honesty).
        """
        if self._memory is None:
            return specs
        cited = self._memory.similar(case_id, k=1)
        if not cited:
            return specs
        top = cited[0].case_id
        out: list[ActionSpec] = []
        for spec in specs:
            if spec.kind is ActionKind.WORK_NOTE and isinstance(spec.payload.get("text"), str):
                payload = {
                    **spec.payload,
                    "text": f"{spec.payload['text']} / similar past case {top}",
                    "similar_case_id": top,
                }
                out.append(spec.model_copy(update={"payload": payload}))
            else:
                out.append(spec)
        return out

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


__all__ = [
    "TriageCaseRepo",
    "TriageClassifier",
    "TriageGrouping",
    "TriageMemory",
    "TriageRunner",
    "TriageService",
    "TriageSpend",
]
