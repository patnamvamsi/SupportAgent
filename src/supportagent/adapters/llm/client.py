"""LLMClient: the single structured-LLM boundary (01 §5-§6, INV-06).

``LLMClient.structured`` is the one place a ticket's text becomes a prompt. It:

1. renders the slot prompt (strict, versioned, untrusted-escaped);
2. redacts system + user (INV-06: no secret survives past this point);
3. records a ``redact`` event (per-class *counts* only — never a value);
4. calls the provider, then parses the payload into a domain model;
5. records an ``llm`` event with a deterministic ``rendered_prompt_hash``;
6. returns the model, or ``None`` — it **never raises** to the caller (01 §6:
   timeouts, rate limits, unavailability, and a schema-invalid payload all
   collapse to ``None`` + an ``error`` event).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar

from pydantic import BaseModel

from supportagent.adapters.llm.prompts import PromptRegistry
from supportagent.adapters.llm.redact import RedactionResult, Redactor
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import payload_hash
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.event import Event
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.llm import LLMProvider, ProviderRequest

TModel = TypeVar("TModel", bound=BaseModel)

_EventStatus = Literal["ok", "error", "skipped"]


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """Per-slot LLM configuration (model, deadline, output budget)."""

    model_id: str
    timeout_s: float
    max_output_tokens: int


class LLMClient:
    """Render → redact → call → parse, recording events and never raising (01 §6)."""

    def __init__(  # noqa: PLR0913 — the six collaborators are the boundary's contract
        self,
        *,
        provider: LLMProvider,
        redactor: Redactor,
        prompts: PromptRegistry,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
        slots: Mapping[SlotName, SlotSpec],
    ) -> None:
        self._provider = provider
        self._redactor = redactor
        self._prompts = prompts
        self._events = events
        self._ids = ids
        self._clock = clock
        self._slots = dict(slots)

    def structured(
        self,
        slot: SlotName,
        variables: Mapping[str, str],
        schema: type[TModel],
        trace: TraceContext,
    ) -> TModel | None:
        """One slot call (02 §5). Returns a validated model, or ``None``; never raises."""
        spec = self._slots.get(slot)
        if spec is None:
            return None
        try:
            rendered = self._prompts.render(slot, variables)
        except Exception:  # noqa: BLE001 — a missing prompt is a load error, not a caller concern
            return None

        system_result = self._redactor.redact(rendered.system)
        user_result = self._redactor.redact(rendered.user)

        self._emit_redact(slot, self._aggregate(system_result, user_result), trace)

        rendered_prompt_hash = payload_hash(
            {
                "slot": slot.value,
                "version": rendered.version,
                "system": system_result.text,
                "user": user_result.text,
            }
        )

        request = ProviderRequest(
            model_id=spec.model_id,
            system=system_result.text,
            user=user_result.text,
            json_schema=schema.model_json_schema(),
            max_output_tokens=spec.max_output_tokens,
            timeout_s=spec.timeout_s,
        )

        try:
            response = self._provider.complete_json(request)
        except Exception as exc:  # noqa: BLE001 — the boundary must never raise (01 §6)
            self._emit_llm(
                slot,
                trace,
                "error",
                spec.model_id,
                0,
                0,
                0,
                rendered_prompt_hash,
                type(exc).__name__,
            )
            return None

        try:
            model = schema.model_validate_json(response.raw_text)
        except Exception as exc:  # noqa: BLE001 — a bad payload is a failed call, not a crash
            self._emit_llm(
                slot,
                trace,
                "error",
                response.resolved_model,
                response.tokens_in,
                response.tokens_out,
                response.latency_ms,
                rendered_prompt_hash,
                type(exc).__name__,
            )
            return None

        self._emit_llm(
            slot,
            trace,
            "ok",
            response.resolved_model,
            response.tokens_in,
            response.tokens_out,
            response.latency_ms,
            rendered_prompt_hash,
            None,
        )
        return model

    @staticmethod
    def _aggregate(system_result: RedactionResult, user_result: RedactionResult) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cls, count in system_result.counts_by_class.items():
            counts[cls] = counts.get(cls, 0) + count
        for cls, count in user_result.counts_by_class.items():
            counts[cls] = counts.get(cls, 0) + count
        return counts

    def _emit_redact(self, slot: SlotName, counts: dict[str, int], trace: TraceContext) -> None:
        self._events.append(
            self._build_event(
                kind=EventKind.REDACT,
                trace=trace,
                status="ok",
                model=None,
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                payload={"v": 1, "slot": slot.value, "counts_by_class": counts},
            )
        )

    def _emit_llm(  # noqa: PLR0913, PLR0917 — flat event fields keep the call sites readable
        self,
        slot: SlotName,
        trace: TraceContext,
        status: _EventStatus,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        rendered_prompt_hash: str,
        error: str | None,
    ) -> None:
        payload: dict[str, object] = {
            "v": 1,
            "slot": slot.value,
            "rendered_prompt_hash": rendered_prompt_hash,
        }
        if error is not None:
            payload["error"] = error
        self._events.append(
            self._build_event(
                kind=EventKind.LLM,
                trace=trace,
                status=status,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                payload=payload,
            )
        )

    def _build_event(  # noqa: PLR0913 — the Event field set is the contract
        self,
        *,
        kind: EventKind,
        trace: TraceContext,
        status: _EventStatus,
        model: str | None,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        payload: dict[str, object],
    ) -> Event:
        return Event(
            id=self._ids.new(IdPrefix.EVENT),
            tenant_id=trace.tenant_id,
            trace_id=trace.trace_id,
            span_id=trace.span_id,
            parent_span_id=trace.parent_span_id,
            kind=kind,
            actor="agent",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            status=status,
            payload=payload,
            ts=self._clock.now(),
        )
