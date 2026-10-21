"""BL-006 (Ollama provider) and BL-008 (provider-native JSON mode), 01 §5-§6."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from supportagent.adapters.llm.providers import make_llm_provider
from supportagent.adapters.llm.providers.anthropic import AnthropicProvider
from supportagent.adapters.llm.providers.ollama import OllamaProvider
from supportagent.adapters.llm.providers.openai import OpenAIProvider
from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest

SCHEMA = {
    "type": "object",
    "properties": {"classification": {"type": "string"}, "severity": {"type": "string"}},
    "required": ["classification", "severity"],
}
ANSWER = {"classification": "network/dns", "severity": "P3"}


def _request(model: str = "llama3.1:8b-instruct-q4_K_M") -> ProviderRequest:
    return ProviderRequest(
        model_id=model,
        system="system prompt",
        user="user prompt",
        json_schema=SCHEMA,
        max_output_tokens=400,
        timeout_s=15.0,
    )


def _ollama(handler: Any) -> OllamaProvider:
    return OllamaProvider(
        httpx.Client(base_url="http://ollama.test", transport=httpx.MockTransport(handler))
    )


def test_ollama_sends_the_schema_and_maps_usage() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "llama3.1:8b-instruct-q4_K_M",
                "message": {"role": "assistant", "content": json.dumps(ANSWER)},
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        )

    resp = _ollama(handler).complete_json(_request())
    assert json.loads(resp.raw_text) == ANSWER
    assert (resp.tokens_in, resp.tokens_out) == (120, 30)
    body = seen[0]
    assert body["format"] == SCHEMA
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 400
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


@pytest.mark.parametrize(
    ("respond", "error"),
    [
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), LLMTimeout),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)), LLMUnavailable),
        (lambda _: httpx.Response(429), LLMRateLimited),
        (lambda _: httpx.Response(500, text="model crashed"), LLMUnavailable),
        (lambda _: httpx.Response(404, text="model not found"), LLMUnavailable),
        (lambda _: httpx.Response(200, json={"no": "message"}), LLMUnavailable),
    ],
)
def test_ollama_maps_errors_to_the_domain(respond: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        _ollama(respond).complete_json(_request())


def test_ollama_is_a_named_provider() -> None:
    client = httpx.Client(base_url="http://ollama.test")
    assert isinstance(make_llm_provider("ollama", client), OllamaProvider)


class _Capture:
    """A fake SDK client that records ``create`` kwargs and returns a canned response."""

    def __init__(self, response: Any) -> None:
        self.kwargs: dict[str, Any] = {}
        self._response = response
        self.messages = self
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self._response


def test_anthropic_forces_a_tool_with_the_schema() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="emit_result", input=ANSWER)],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    client = _Capture(response)
    out = AnthropicProvider(client).complete_json(  # type: ignore[arg-type]
        _request("claude-sonnet-4-5-20250929")
    )
    assert json.loads(out.raw_text) == ANSWER
    (tool,) = client.kwargs["tools"]
    assert tool["input_schema"] == SCHEMA
    assert client.kwargs["tool_choice"] == {"type": "tool", "name": tool["name"]}


def test_anthropic_falls_back_to_text_without_a_tool_block() -> None:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(ANSWER))],
        model="claude-sonnet-4-5-20250929",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    out = AnthropicProvider(_Capture(response)).complete_json(  # type: ignore[arg-type]
        _request("claude-sonnet-4-5-20250929")
    )
    assert json.loads(out.raw_text) == ANSWER


def test_openai_sends_a_json_schema_response_format() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(ANSWER)))],
        model="gpt-4o-2024-08-06",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )
    client = _Capture(response)
    out = OpenAIProvider(client).complete_json(_request("gpt-4o-2024-08-06"))  # type: ignore[arg-type]
    assert json.loads(out.raw_text) == ANSWER
    fmt = client.kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMA
