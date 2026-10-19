"""Anthropic structured-LLM provider (01 §5, 01 §6).

Maps the Anthropic SDK's exception hierarchy onto the domain taxonomy so
application code never branches on SDK identity (01 §6: "retryability is typed,
not guessed"). Catch order is most-specific-first: ``APITimeoutError`` before
``APIConnectionError`` (timeout is a connection error), ``RateLimitError``
before ``APIStatusError`` (rate limit is a status error).
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest, ProviderResponse


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...  # noqa: ANN401 — the SDK response shape


class _AnthropicClient(Protocol):
    messages: _Messages


class AnthropicProvider:
    """One JSON-mode completion against the Anthropic Messages API (01 §5)."""

    def __init__(self, client: _AnthropicClient) -> None:
        self._client = client

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        try:
            response = self._client.messages.create(
                model=request.model_id,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                timeout=request.timeout_s,
            )
        except anthropic.APITimeoutError as exc:
            raise LLMTimeout(f"anthropic timed out: {exc}") from exc  # noqa: TRY003
        except anthropic.RateLimitError as exc:
            raise LLMRateLimited(f"anthropic rate limited: {exc}") from exc  # noqa: TRY003
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"anthropic unavailable: {exc}") from exc  # noqa: TRY003
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"anthropic connection failed: {exc}") from exc  # noqa: TRY003

        text = "".join(getattr(block, "text", "") for block in response.content)
        usage = response.usage
        return ProviderResponse(
            raw_text=text,
            resolved_model=response.model,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            latency_ms=0,
        )
