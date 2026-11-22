"""TraceExplainer (ADR-0021 §5 steps 3-5): from a verdict to a cited answer.

1. **Scoped RCA.** When the object is stuck or rejected at a stage whose
   component is a catalog service, RCA runs on that service, with the stage's
   ``rca_class`` when the flow names one. Its supported causes become facts,
   pseudonymised like the evidence. Without a PII analyser they are left out:
   RCA's claims quote tool output.
2. **Facts.** The stages, the verdict, the evidence lines and the causes,
   numbered (``domain.flow_answer``). Nothing else reaches the model.
3. **The answer.** The ``explain_trace`` slot writes sentences for the
   engineer and for the business, each citing facts. A sentence citing an
   unknown fact is dropped; every other one is checked against the facts it
   cites (``check_trace``) and kept only on ``supports``.
4. **Fallback.** The template answer from the verdict, which needs no model,
   is always there; it stands in when no business sentence survives.

The verdict is never the model's: it comes from the tracer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC
from typing import Literal

from pydantic import Field

from supportagent.app.rca import Budget, RcaArtifact, RcaCase
from supportagent.app.tracing import TraceRun
from supportagent.domain.errors import PiiUnavailable
from supportagent.domain.flow_answer import Fact, facts, template_answer, valid_cites
from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.catalog import CatalogView
from supportagent.domain.model.flow import Flow
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.llm import StructuredLLM

#: RCA runs only where a stage is where the object stopped on its own.
_RCA_VERDICTS = frozenset({"stuck", "rejected"})


class Sentence(DomainModel):
    text: str = Field(min_length=1, max_length=400)
    cites: list[str] = Field(min_length=1, max_length=8)


class TraceExplanation(DomainModel):
    engineer: list[Sentence] = Field(default_factory=list, max_length=8)
    business: list[Sentence] = Field(default_factory=list, max_length=4)


class SentenceCheck(DomainModel):
    verdict: Literal["supports", "contradicts", "unknown"]


@dataclass(frozen=True, slots=True)
class TraceAnswer:
    template: str
    facts: tuple[Fact, ...]
    engineer: tuple[Sentence, ...] = ()
    business: tuple[Sentence, ...] = ()
    #: Sentences the checks dropped (bad cites, or not supported by them).
    dropped: int = 0
    rca: Mapping[str, object] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """The business answer, or the template when no sentence survived."""
        return " ".join(s.text for s in self.business) or self.template

    def as_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "template": self.template,
            "engineer": [{"text": s.text, "cites": s.cites} for s in self.engineer],
            "business": [{"text": s.text, "cites": s.cites} for s in self.business],
            "facts": [{"id": f.id, "text": f.text} for f in self.facts],
            "dropped": self.dropped,
            "rca": dict(self.rca),
        }


RunRca = Callable[[RcaCase, Budget], RcaArtifact]


class TraceExplainer:
    def __init__(
        self,
        *,
        llm: StructuredLLM | None,
        catalog: CatalogView,
        rca: RunRca | None = None,
        budget: Callable[[], Budget] = Budget,
    ) -> None:
        self._llm = llm
        self._services = {s.name: s for s in catalog.services}
        self._rca = rca
        self._budget = budget

    def explain(
        self,
        flow: Flow,
        run: TraceRun,
        trace: TraceContext,
        *,
        pseudonymise: Callable[[str], str] | None,
    ) -> TraceAnswer:
        causes, rca = self._causes(flow, run, trace, pseudonymise)
        known = facts(flow, run.object_id, run.observations, run.verdict, run.evidence, causes)
        template = template_answer(flow, run.object_id, run.verdict)
        if self._llm is None:
            return TraceAnswer(template, tuple(known), rca=rca)
        got = self._llm.structured(
            SlotName.EXPLAIN_TRACE,
            {
                "object": flow.object,
                "object_id": run.object_id,
                "flow": flow.title,
                "facts": _facts_block(known),
            },
            TraceExplanation,
            trace,
        )
        if got is None:
            return TraceAnswer(template, tuple(known), rca=rca)
        engineer, d1 = self._checked(got.engineer, known, trace)
        business, d2 = self._checked(got.business, known, trace)
        return TraceAnswer(template, tuple(known), engineer, business, d1 + d2, rca)

    def _checked(
        self, sentences: list[Sentence], known: list[Fact], trace: TraceContext
    ) -> tuple[tuple[Sentence, ...], int]:
        assert self._llm is not None  # noqa: S101  # nosec B101 — explain() checked it
        by_id = {f.id: f for f in known}
        kept: list[Sentence] = []
        for s in sentences:
            if not valid_cites(s.cites, known):
                continue
            check = self._llm.structured(
                SlotName.CHECK_TRACE,
                {"sentence": s.text, "facts": _facts_block([by_id[c] for c in s.cites])},
                SentenceCheck,
                trace,
            )
            if check is not None and check.verdict == "supports":
                kept.append(s)
        return tuple(kept), len(sentences) - len(kept)

    def _causes(
        self,
        flow: Flow,
        run: TraceRun,
        trace: TraceContext,
        pseudonymise: Callable[[str], str] | None,
    ) -> tuple[list[str], dict[str, object]]:
        v = run.verdict
        if self._rca is None or v.kind not in _RCA_VERDICTS or v.stage is None:
            return [], {}
        stage = next(s for s in flow.stages if s.id == v.stage)
        service = self._services.get(stage.component.split(":", 1)[-1])
        if service is None:
            return [], {"skipped": f"{stage.component} isn't a catalog service"}
        since = v.since or run.observations[0].at
        case = RcaCase(
            id=f"{trace.trace_id}_rca",
            tenant_id=trace.tenant_id,
            trace_id=trace.trace_id,
            service=service,
            classification=stage.rca_class,
            severity=None,
            origin="trace",
            opened_ts=int(since.astimezone(UTC).timestamp()) if since else 0,
            summary=f"{flow.object} {run.object_id}: {v.kind} at stage '{stage.title}'",
        )
        artifact = self._rca(case, self._budget())
        summary: dict[str, object] = {
            "service": service.name,
            "outcome": artifact.outcome,
            "supported_by": artifact.supported_by,
        }
        if pseudonymise is None:
            summary["causes_withheld"] = "no PII analyser: RCA's claims quote tool output"
            return [], summary
        try:
            causes = [
                pseudonymise(c.text)
                for c in artifact.claims
                if not c.tags & {"speculation", "contradicted"}
            ]
        except PiiUnavailable as exc:
            summary["causes_withheld"] = str(exc)
            return [], summary
        return causes, summary


def _facts_block(known: list[Fact]) -> str:
    return "\n".join(f"[{f.id}] {f.text}" for f in known)


__all__ = ["Sentence", "TraceAnswer", "TraceExplainer", "TraceExplanation"]
