"""LLM port (01 §5, 02 §5): the structured-LLM boundary, provider-agnostic.

Two protocols live here:

- ``LLMProvider`` — the adapter-facing seam: one JSON-mode completion. SDK
  errors are mapped to the domain taxonomy *inside the provider* (01 §6:
  "retryability is typed, not guessed"), so application code never branches
  on SDK identity.
- ``StructuredLLM`` — the application-facing seam: render the slot prompt,
  redact (INV-06), call the provider, parse into a domain model. ``None``
  means "no usable answer" (timeout, rate limit, unavailable, or a payload
  that fails schema validation); it never raises.

The ports layer names only domain types (no pydantic, no SDK).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar

from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext

TModel = TypeVar("TModel")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """A provider-agnostic structured-LLM request (01 §5)."""

    model_id: str
    system: str
    user: str
    json_schema: Mapping[str, object]
    max_output_tokens: int
    timeout_s: float


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A provider-agnostic structured-LLM response (raw JSON + usage)."""

    raw_text: str
    resolved_model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


class LLMProvider(Protocol):
    """A provider of structured (JSON-mode) LLM completions (01 §5)."""

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        """One completion; SDK errors surface as domain errors (01 §6)."""
        ...


class StructuredLLM(Protocol):
    """The application-facing LLM boundary (02 §5)."""

    def structured(
        self,
        slot: SlotName,
        variables: Mapping[str, str],
        schema: type[TModel],
        trace: TraceContext,
    ) -> TModel | None:
        """One slot call.

        Returns a validated ``schema`` instance, or ``None`` when the provider
        failed or its payload failed validation. Never raises: timeouts,
        rate limits, and unavailability are typed, not thrown (01 §6).
        """
        ...
