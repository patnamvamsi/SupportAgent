"""LLM provider adapters (01 §5, 01 §6).

Re-exports the concrete providers and a name-based factory so bootstrap code
wires the right provider without importing the SDK directly.
"""

from __future__ import annotations

from typing import Any

from supportagent.adapters.llm.providers.anthropic import AnthropicProvider
from supportagent.adapters.llm.providers.openai import OpenAIProvider
from supportagent.domain.errors import ConfigError
from supportagent.ports.llm import LLMProvider

__all__ = ["AnthropicProvider", "OpenAIProvider", "make_llm_provider"]


def make_llm_provider(name: str, client: Any) -> LLMProvider:  # noqa: ANN401
    """Construct an ``LLMProvider`` by name (01 §5). The SDK client is provider-specific."""
    if name == "anthropic":
        return AnthropicProvider(client)
    if name == "openai":
        return OpenAIProvider(client)
    raise ConfigError(f"unknown LLM provider: {name}")  # noqa: TRY003
