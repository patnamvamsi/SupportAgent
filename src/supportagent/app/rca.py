"""RCA-lite (ARCH §5, §5.4, §5.5; 11-wp WP-110): a budgeted hypothesis-evidence loop.

``RcaService.run(case, budget)`` follows the ARCH §5 pseudo-code:

1. **Plan.** A matching runbook *is* the plan, in step order. Otherwise the
   ``propose_hypotheses`` slot may name taxonomy ids only. They're ordered by
   the prior (``order_by_prior``; a freeze zeroes ``recent_change_push`` and
   writes a ``prior_zeroed`` event), capped at ``budget.max_hypotheses``, and
   kept only when a **registered probe** exists. No plan → INCONCLUSIVE.
2. **Steps.** Each step calls its tool through the registry (arguments bound
   from the catalog service) and evaluates its compiled CEL check over the
   typed result. A step whose tool isn't registered, whose arguments can't be
   bound, or whose call fails *did not run*: it can never support anything
   (§5.5). ``stop_if`` ends the plan on a satisfied check. Budget exhaustion
   stops it, and is logged.
3. **Reduce, conclude, entail.** ``domain.rca_reduce`` gives the categorical
   outcome. Each claim then goes through the ``entail`` slot; anything but
   ``supports`` (including a failure or the claim/time budget) tags it
   ``speculation``, and ``contradicts`` also tags it ``contradicted``. The
   outcome never changes after ``reduce``.

``RcaJob`` is the ``rca.run`` worker job: open cases routed to an
``rca: lite`` route, one RCA per storm group (the lead only), one artifact
per case.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa
from pydantic import Field

from supportagent.domain.errors import DomainValidationError, ToolError
from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.event import Event
from supportagent.domain.model.runbook import Probe, RcaBudget, Runbook
from supportagent.domain.rca_reduce import (
    CHANGE_HYPOTHESIS,
    Claim,
    Outcome,
    StepResult,
    apply_verdict,
    conclude,
    order_by_prior,
    reduce,
)
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.checks import CompiledCheck
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.llm import StructuredLLM
from supportagent.ports.tools import ToolResult

#: A rough token charge per LLM call, against ``budget.max_tokens`` (prov.).
PROPOSE_TOKENS = 400
ENTAIL_BASE_TOKENS = 200
_EVIDENCE_CHARS = 4000


class HypothesisSet(DomainModel):
    hypotheses: list[str] = Field(default_factory=list, max_length=10)


class Entailment(DomainModel):
    verdict: Literal["supports", "contradicts", "unknown"]


@dataclass(frozen=True, slots=True)
class RcaCase:
    id: str
    tenant_id: str
    trace_id: str
    service: ServiceEntry | None
    classification: str | None
    severity: str | None
    origin: str
    opened_ts: int

    def as_cel(self) -> dict[str, object]:
        return {
            "id": self.id,
            "service": self.service.name if self.service else None,
            "severity": self.severity,
            "classification": self.classification,
            "origin": self.origin,
            "opened_ts": self.opened_ts,
        }


@dataclass(slots=True)
class Budget:
    max_tool_calls: int = 5
    max_tokens: int = 3000
    max_hypotheses: int = 3
    tool_calls: int = 0
    tokens: int = 0

    @classmethod
    def of(cls, spec: RcaBudget) -> Budget:
        return cls(spec.max_tool_calls, spec.max_tokens, spec.max_hypotheses)

    def exhausted(self) -> bool:
        return self.tool_calls >= self.max_tool_calls or self.tokens > self.max_tokens

    def can_spend(self, tokens: int) -> bool:
        return self.tokens + tokens <= self.max_tokens


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    hypothesis: str
    tool: str
    args: Mapping[str, str | int]
    check: CompiledCheck
    stop_if: bool = False


@dataclass(frozen=True, slots=True)
class Evidence:
    step: StepResult
    tool: str
    result: Mapping[str, object] | None


@dataclass(slots=True)
class RcaArtifact:
    outcome: Outcome
    reason: str
    plan: str  # "runbook:<id>" | "proposed" | "none"
    hypotheses: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    supported_by: str | None = None
    comms_template: str | None = None  # internal only (ARCH §5)
    budget: dict[str, int] = field(default_factory=dict)

    @property
    def score(self) -> float | None:
        """Derived and labeled for reporting; never the decision variable (§5)."""
        ran = [e for e in self.evidence if e.step.ran]
        return round(sum(e.step.ok for e in ran) / len(ran), 3) if ran else None

    def as_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "outcome": self.outcome,
            "reason": self.reason,
            "plan": self.plan,
            "hypotheses": self.hypotheses,
            "supported_by": self.supported_by,
            "evidence": [
                {
                    "step": e.step.step_id,
                    "hypothesis": e.step.hypothesis,
                    "tool": e.tool,
                    "ran": e.step.ran,
                    "ok": e.step.ok,
                    "why": e.step.why,
                }
                for e in self.evidence
            ],
            "claims": [
                {"text": c.text, "cites": c.cites, "tags": sorted(c.tags), "sendable": c.sendable}
                for c in self.claims
            ],
            "score": {"label": "share of run checks that held", "value": self.score},
            "comms_if_supported": (
                {"audience": ["internal"], "template": self.comms_template}
                if self.comms_template
                else None
            ),
            "budget": self.budget,
        }


class _Tools(Protocol):
    @property
    def names(self) -> frozenset[str]: ...

    def call(
        self,
        name: str,
        template: Mapping[str, object],
        service: ServiceEntry,
        trace: TraceContext,
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class Knowledge:
    """The loaded runbooks, probes and hypothesis conflict set."""

    runbooks: Sequence[tuple[Runbook, Mapping[str, CompiledCheck]]]
    probes: Mapping[str, tuple[Probe, CompiledCheck]]
    excludes: Mapping[str, Sequence[str]]

    @property
    def hypothesis_ids(self) -> list[str]:
        return list(self.excludes)

    def match(self, case: RcaCase) -> tuple[Runbook, Mapping[str, CompiledCheck]] | None:
        label = case.classification
        if label is None:
            return None
        for runbook, checks in self.runbooks:
            want = runbook.applies_when.classification
            if (label == want or label.startswith(want + "/")) and (
                case.severity in runbook.applies_when.severities
            ):
                return runbook, checks
        return None


class RcaService:
    def __init__(  # noqa: PLR0913 — the ports plus the §5.5 prior
        self,
        *,
        knowledge: Knowledge,
        tools: _Tools,
        llm: StructuredLLM,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
        prior: Mapping[str, float],
        freeze: bool = False,
        max_claims: int = 5,
        entail_budget_s: float = 20.0,
    ) -> None:
        self._kb = knowledge
        self._tools = tools
        self._llm = llm
        self._events = events
        self._ids = ids
        self._clock = clock
        self._prior = dict(prior)
        self._freeze = freeze
        self._max_claims = max_claims
        self._entail_budget_s = entail_budget_s

    def budget_for(self, case: RcaCase, default: RcaBudget) -> Budget:
        matched = self._kb.match(case)
        return Budget.of(matched[0].budget if matched else default)

    def run(self, case: RcaCase, budget: Budget) -> RcaArtifact:
        trace = TraceContext(case.tenant_id, case.trace_id, f"rca_{case.id}")
        matched = self._kb.match(case)
        if matched is not None:
            runbook, checks = matched
            plan = [
                PlanStep(s.id, s.hypothesis, s.tool, s.args, checks[s.id], s.stop_if)
                for s in runbook.steps
            ]
            source = runbook.id
        else:
            runbook = None
            plan = self._proposed_plan(case, budget, trace)
            source = "proposed"
            if not plan:
                self._decision(trace, case, "INCONCLUSIVE", "no_runbook_no_probes")
                return RcaArtifact(
                    "INCONCLUSIVE", "no testable plan", "none", budget=_spent(budget)
                )

        evidence: list[Evidence] = []
        for step in plan:
            if budget.exhausted():
                self._decision(trace, case, None, "budget_stop")
                break
            ev = self._run_step(case, step, budget, trace)
            evidence.append(ev)
            if ev.step.ok and step.stop_if:
                break

        results = [e.step for e in evidence]
        reduction = reduce(results, plan_length=len(plan), excludes=self._kb.excludes)
        claims = conclude(results)
        self._entail(claims, evidence, budget, trace)
        artifact = RcaArtifact(
            outcome=reduction.outcome,
            reason=reduction.reason,
            plan=source,
            hypotheses=list(dict.fromkeys(p.hypothesis for p in plan)),
            evidence=evidence,
            claims=claims,
            supported_by=reduction.supported_by,
            comms_template=(
                runbook.comms_if_supported.template
                if runbook is not None
                and runbook.comms_if_supported is not None
                and reduction.outcome == "SUPPORTED"
                else None
            ),
            budget=_spent(budget),
        )
        self._decision(trace, case, artifact.outcome, artifact.reason)
        return artifact

    # -- planning ------------------------------------------------------------

    def _proposed_plan(self, case: RcaCase, budget: Budget, trace: TraceContext) -> list[PlanStep]:
        ids = self._kb.hypothesis_ids
        proposed: list[str] = []
        if budget.can_spend(PROPOSE_TOKENS):
            budget.tokens += PROPOSE_TOKENS
            got = self._llm.structured(
                SlotName.PROPOSE_HYPOTHESES,
                {
                    "hypothesis_ids": ", ".join(ids),
                    "max_hypotheses": str(budget.max_hypotheses),
                    "service": case.service.name if case.service else "unknown",
                    "classification": case.classification or "unknown",
                    "severity": case.severity or "unknown",
                },
                HypothesisSet,
                trace,
            )
            proposed = [h for h in (got.hypotheses if got else []) if h in ids]
        ordered, zeroed = order_by_prior(proposed, self._prior, freeze=self._freeze)
        if zeroed:
            self._emit(
                trace,
                EventKind.PRIOR_ZEROED,
                {"v": 1, "case_id": case.id, "hypothesis": CHANGE_HYPOTHESIS, "why": "freeze"},
            )
        plan: list[PlanStep] = []
        for hypothesis in ordered[: budget.max_hypotheses]:
            probe = self._kb.probes.get(hypothesis)
            if probe is None:
                self._decision(trace, case, None, f"no_probe:{hypothesis}")
                continue
            spec, check = probe
            plan.append(PlanStep(f"probe-{hypothesis}", hypothesis, spec.tool, spec.args, check))
        return plan

    # -- steps ---------------------------------------------------------------

    def _run_step(
        self, case: RcaCase, step: PlanStep, budget: Budget, trace: TraceContext
    ) -> Evidence:
        def failed(why: str) -> Evidence:
            result = StepResult(step.step_id, step.hypothesis, ran=False, ok=False, why=why)
            self._step_event(trace, case, step, result)
            return Evidence(result, step.tool, None)

        if step.tool not in self._tools.names:
            return failed(f"tool {step.tool} is not registered (§5.5)")
        if case.service is None:
            return failed("the case has no catalog service to bind arguments from")
        budget.tool_calls += 1
        try:
            out = self._tools.call(step.tool, step.args, case.service, trace)
        except (ToolError, DomainValidationError) as exc:
            return failed(f"tool call failed: {str(exc)[:160]}")
        ok, why = step.check.is_satisfied_by(
            out.data, case.service.model_dump(mode="json"), case.as_cel()
        )
        result = StepResult(step.step_id, step.hypothesis, ran=True, ok=ok, why=why)
        self._step_event(trace, case, step, result)
        return Evidence(result, step.tool, dict(out.data))

    # -- entailment (§5.4) ---------------------------------------------------

    def _entail(
        self,
        claims: Sequence[Claim],
        evidence: Sequence[Evidence],
        budget: Budget,
        trace: TraceContext,
    ) -> None:
        by_step = {e.step.step_id: e for e in evidence}
        started = self._clock.now()
        for index, claim in enumerate(claims):
            elapsed = (self._clock.now() - started).total_seconds()
            cited = [by_step[s].result for s in claim.cites if s in by_step]
            text = json.dumps(cited, sort_keys=True, default=str)[:_EVIDENCE_CHARS]
            cost = ENTAIL_BASE_TOKENS + len(text) // 4
            verdict = None
            if (
                index < self._max_claims
                and elapsed < self._entail_budget_s
                and budget.can_spend(cost)
            ):
                budget.tokens += cost
                got = self._llm.structured(
                    SlotName.ENTAIL, {"claim": claim.text, "evidence": text}, Entailment, trace
                )
                verdict = got.verdict if got else None
            apply_verdict(claim, verdict)

    # -- events --------------------------------------------------------------

    def _step_event(
        self, trace: TraceContext, case: RcaCase, step: PlanStep, result: StepResult
    ) -> None:
        self._emit(
            trace,
            EventKind.RCA_STEP,
            {
                "v": 1,
                "case_id": case.id,
                "step": step.step_id,
                "hypothesis": step.hypothesis,
                "tool": step.tool,
                "status": "ok" if result.ok else ("not_ok" if result.ran else "not_run"),
                "why": result.why[:300],
            },
        )

    def _decision(
        self, trace: TraceContext, case: RcaCase, outcome: str | None, reason: str
    ) -> None:
        self._emit(
            trace,
            EventKind.DECISION,
            {"v": 1, "stage": "rca", "case_id": case.id, "outcome": outcome, "reason": reason},
        )

    def _emit(self, trace: TraceContext, kind: EventKind, payload: dict[str, object]) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        self._events.append(
            Event(
                id=event_id,
                tenant_id=trace.tenant_id,
                trace_id=trace.trace_id,
                span_id=f"rca_{event_id}",
                parent_span_id=None,
                kind=kind,
                actor="agent",
                payload=payload,
                ts=self._clock.now(),
            )
        )


class BufferedEvents:
    """Holds RCA events until ``RcaJob.save`` writes them with the artifact.

    RCA calls tools over the network; buffering keeps the database
    transaction short (one write per case, not one held across tool calls).
    """

    def __init__(self) -> None:
        self._held: list[Event] = []

    def append(self, event: Event) -> None:
        self._held.append(event)

    def drain(self) -> list[Event]:
        held, self._held = self._held, []
        return held


def _spent(budget: Budget) -> dict[str, int]:
    return {"tool_calls": budget.tool_calls, "tokens": budget.tokens}


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


_CANDIDATES = (
    "SELECT c.id, c.trace_id, c.service, c.classification, c.severity, c.origin, "
    "c.opened_ts, c.decision_json FROM cases c "
    "WHERE c.tenant_id = :t AND c.status <> 'resolved' "
    "AND NOT EXISTS (SELECT 1 FROM rca_artifacts r WHERE r.case_id = c.id) "
    "AND NOT EXISTS (SELECT 1 FROM cases o WHERE o.group_id = c.group_id "
    "AND (o.opened_ts < c.opened_ts OR (o.opened_ts = c.opened_ts AND o.id < c.id))) "
    "ORDER BY c.opened_ts, c.id"
)
_INSERT = (
    "INSERT INTO rca_artifacts (id, tenant_id, case_id, outcome, artifact_json, created_ts) "
    "VALUES (:id, :t, :c, :o, :a, :ts)"
)


class RcaJob:
    """``rca.run``: RCA-lite for open, ``rca: lite``-routed cases; group leads only."""

    def __init__(  # noqa: PLR0913 — the ports plus the routing view
        self,
        *,
        uow: _UoW,
        service: RcaService,
        catalog: Catalog,
        buffer: BufferedEvents,
        events: EventSink,
        lite_routes: Collection[str],
        default_budget: RcaBudget,
        ids: IdGenerator,
        clock: Clock,
        tenant_id: str,
        max_per_run: int = 10,
    ) -> None:
        self._uow = uow
        self._service = service
        self._services = {s.name: s for s in catalog.services}
        self._buffer = buffer
        self._events = events
        self._lite = frozenset(lite_routes)
        self._default = default_budget
        self._ids = ids
        self._clock = clock
        self._tenant = tenant_id
        self._max = max_per_run

    def due(self) -> list[RcaCase]:
        if not self._lite:
            return []
        with self._uow:
            conn = self._uow.connection
            rows = conn.execute(sa.text(_CANDIDATES), {"t": self._tenant}).mappings().all()
            out: list[RcaCase] = []
            for row in rows:
                decision = json.loads(row["decision_json"] or "{}")
                if (
                    decision.get("outcome") != "routed"
                    or decision.get("route_id") not in self._lite
                ):
                    continue
                out.append(
                    RcaCase(
                        id=str(row["id"]),
                        tenant_id=self._tenant,
                        trace_id=str(row["trace_id"]),
                        service=self._services.get(row["service"] or ""),
                        classification=row["classification"],
                        severity=row["severity"],
                        origin=str(row["origin"]),
                        opened_ts=int(row["opened_ts"]),
                    )
                )
                if len(out) >= self._max:
                    break
        return out

    def run_once(self) -> int:
        done = 0
        for case in self.due():
            self._buffer.drain()
            artifact = self._service.run(case, self._service.budget_for(case, self._default))
            self.save(case, artifact)
            done += 1
        return done

    def save(self, case: RcaCase, artifact: RcaArtifact) -> str:
        artifact_id = self._ids.new(IdPrefix.RCA)
        with self._uow:
            for event in self._buffer.drain():
                self._events.append(event)
            self._uow.connection.execute(
                sa.text(_INSERT),
                {
                    "id": artifact_id,
                    "t": self._tenant,
                    "c": case.id,
                    "o": artifact.outcome,
                    "a": json.dumps(artifact.as_dict(), sort_keys=True),
                    "ts": int(self._clock.now().astimezone(UTC).timestamp()),
                },
            )
            self._uow.commit()
        return artifact_id


def lite_route_ids(routes: Mapping[str, str]) -> frozenset[str]:
    """Route ids whose ``rca`` is ``lite``, from ``{route_id: rca}``."""
    return frozenset(rid for rid, rca in routes.items() if rca == "lite")


__all__ = [
    "Budget",
    "BufferedEvents",
    "Entailment",
    "Evidence",
    "HypothesisSet",
    "Knowledge",
    "PlanStep",
    "RcaArtifact",
    "RcaCase",
    "RcaJob",
    "RcaService",
    "lite_route_ids",
]
