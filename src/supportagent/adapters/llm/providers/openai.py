"""OpenAI structured-LLM provider (01 §5, 01 §6).

Maps the OpenAI SDK's exception hierarchy onto the domain taxonomy so
application code never branches on SDK identity (01 §6: "retryability is typed,
not guessed"). Catch order is most-specific-first: ``APITimeoutError`` before
``APIConnectionError`` (timeout is a connection error), ``RateLimitError``
before ``APIStatusError`` (rate limit is a status error).
"""

from __future__ import annotations

from typing import Any, Protocol

import openai

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest, ProviderResponse


class _Completions(Protocol):
    def create(self, **kwargs: Any) -> Any: ...  # noqa: ANN401 — the SDK response shape


class _Chat(Protocol):
    completions: _Completions


class _OpenAIClient(Protocol):
    chat: _Chat


class OpenAIProvider:
    """One JSON-mode completion against the OpenAI Chat Completions API (01 §5)."""

    def __init__(self, client: _OpenAIClient) -> None:
        self._client = client

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ]
        try:
            response = self._client.chat.completions.create(
                model=request.model_id,
                max_tokens=request.max_output_tokens,
                messages=messages,
                timeout=request.timeout_s,
            )
        except openai.APITimeoutError as exc:
            raise LLMTimeout(f"openai timed out: {exc}") from exc  # noqa: TRY003
        except openai.RateLimitError as exc:
            raise LLMRateLimited(f"openai rate limited: {exc}") from exc  # noqa: TRY003
        except openai.APIStatusError as exc:
            raise LLMUnavailable(f"openai unavailable: {exc}") from exc  # noqa: TRY003
        except openai.APIConnectionError as exc:
            raise LLMUnavailable(f"openai connection failed: {exc}") from exc  # noqa: TRY003

        choice = response.choices[0]
        usage = response.usage
        return ProviderResponse(
            raw_text=choice.message.content,
            resolved_model=response.model,
            tokens_in=usage.prompt_tokens,
            tokens_out=usage.completion_tokens,
            latency_ms=0,
        )
