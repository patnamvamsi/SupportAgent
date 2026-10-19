"""Fault injection at the LLM boundary (04 §1, INV-05).

``FaultInjectingLLMProvider`` wraps any provider and, when the
``SUPPORTAGENT_FAULT_INJECT`` environment variable names a known fault, raises
the matching domain error *before* delegating. This lets tests exercise the
client's "never raise, record an error event" contract (01 §6) without a real
network call, and without the inner provider observing the request at all.
"""

from __future__ import annotations

import os

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse

# The three injectable faults, keyed by the env-var value.
_FAULTS: dict[str, type[LLMTimeout | LLMRateLimited | LLMUnavailable]] = {
    "llm_timeout": LLMTimeout,
    "llm_rate_limited": LLMRateLimited,
    "llm_unavailable": LLMUnavailable,
}

_ENV = "SUPPORTAGENT_FAULT_INJECT"


class FaultInjectingLLMProvider:
    """A decorator provider that injects a configured fault (04 §1)."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        fault = os.getenv(_ENV)
        if fault in _FAULTS:
            raise _FAULTS[fault](f"injected fault: {fault}")  # noqa: TRY003
        return self._inner.complete_json(request)
