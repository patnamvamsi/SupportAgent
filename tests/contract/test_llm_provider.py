"""Contract: LLM provider maps SDK errors to the domain error taxonomy (01 §6).

Canonical test ID (10-wp WP-007), parametrized over the four SDK→domain edges:

- ``test_provider_maps_sdk_errors``

Both the Anthropic and OpenAI SDKs expose the *same* exception shape
(``APITimeoutError -> APIConnectionError``, ``RateLimitError -> APIStatusError``).
The provider must map each to the typed domain error so application code never
branches on SDK identity (01 §6: "retryability is typed, not guessed").
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import openai
import pytest

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest


class _RaisingMessages:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **kwargs: Any) -> Any:
        raise self._exc


class _RaisingCompletions:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **kwargs: Any) -> Any:
        raise self._exc


class _RaisingChat:
    def __init__(self, exc: Exception) -> None:
        self.completions = _RaisingCompletions(exc)


class _RaisingClient:
    """A fake SDK client whose ``create`` raises a configured SDK error."""

    def __init__(self, exc: Exception) -> None:
        self.messages = _RaisingMessages(exc)
        self.chat = _RaisingChat(exc)


def _request() -> ProviderRequest:
    return ProviderRequest(
        model_id="claude-sonnet-4-5-20250929",
        system="system prompt",
        user="user prompt",
        json_schema={"type": "object"},
        max_output_tokens=400,
        timeout_s=15.0,
    )


def _provider_for(sdk: str, client: _RaisingClient) -> Any:
    """Build the provider for ``sdk`` over the fake client.

    ``_RaisingClient`` structurally satisfies each provider's private client
    Protocol, but mypy cannot verify a concrete class against a *nested*
    protocol member (``client.messages`` / ``client.chat``) — a known limitation.
    The ignores below are for that, not for real type errors.
    """
    if sdk == "anthropic":
        from supportagent.adapters.llm.providers.anthropic import AnthropicProvider  # noqa: PLC0415

        return AnthropicProvider(client)  # type: ignore[arg-type]
    from supportagent.adapters.llm.providers.openai import OpenAIProvider  # noqa: PLC0415

    return OpenAIProvider(client)  # type: ignore[arg-type]


def _sdk_exception(sdk_error: type[Exception]) -> Exception:
    """Build a real SDK exception (each SDK has a distinct constructor).

    The SDKs depend on ``httpx2`` (a fork), not ``httpx`` — so the request and
    response objects must come from ``httpx2`` to match the exception
    constructor signatures (mypy narrows ``sdk_error`` per branch).
    """
    request = httpx2.Request("POST", "https://api.example.com/v1")
    if sdk_error is anthropic.APITimeoutError:
        return sdk_error(request)
    if sdk_error is anthropic.APIStatusError:
        return sdk_error("boom", response=httpx2.Response(500, request=request), body=None)
    if sdk_error is openai.RateLimitError:
        return sdk_error("rate limited", response=httpx2.Response(429, request=request), body=None)
    if sdk_error is openai.APIConnectionError:
        return sdk_error(request=request)
    raise AssertionError(f"no factory for {sdk_error}")  # noqa: TRY003


_CASES: list[tuple[str, type[Exception], type[Exception]]] = [
    ("anthropic", anthropic.APITimeoutError, LLMTimeout),
    ("anthropic", anthropic.APIStatusError, LLMUnavailable),
    ("openai", openai.RateLimitError, LLMRateLimited),
    ("openai", openai.APIConnectionError, LLMUnavailable),
]
_IDS = [f"{sdk}-{exc.__name__}" for sdk, exc, _domain in _CASES]


@pytest.mark.parametrize(
    ("sdk", "sdk_error", "domain_error"),
    _CASES,
    ids=_IDS,
)
def test_provider_maps_sdk_errors(
    sdk: str, sdk_error: type[Exception], domain_error: type[Exception]
) -> None:
    """A configured SDK error surfaces as the typed domain error, never the SDK."""
    client = _RaisingClient(_sdk_exception(sdk_error))
    provider = _provider_for(sdk, client)
    with pytest.raises(domain_error):
        provider.complete_json(_request())
