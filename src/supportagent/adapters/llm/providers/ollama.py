"""Ollama structured-LLM provider (01 §5, ARCH §9; BL-006): the air-gap path.

One ``POST /api/chat`` to a local Ollama server, non-streaming, with the
response schema passed as ``format`` (Ollama's structured output), so the
model is constrained to the slot's JSON shape. No API key and no outbound
traffic: ``llm.base_url`` points at the local server
(``http://localhost:11434`` by default).

Errors map onto the domain taxonomy (01 §6): a timeout is ``LLMTimeout``,
a 429 is ``LLMRateLimited``, and a connection failure, any other HTTP error
or a malformed body is ``LLMUnavailable``. A local model is slower than a
cloud one; a missed deadline only means more safe defaults.
"""

from __future__ import annotations

import time

import httpx

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest, ProviderResponse

DEFAULT_BASE_URL = "http://localhost:11434"
_RATE_LIMITED = 429


class OllamaProvider:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        body = {
            "model": request.model_id,
            "stream": False,
            "format": dict(request.json_schema),
            "options": {"num_predict": request.max_output_tokens, "temperature": 0},
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        started = time.perf_counter()
        try:
            resp = self._client.post("/api/chat", json=body, timeout=request.timeout_s)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"ollama timed out: {exc}") from exc  # noqa: TRY003
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"ollama unreachable: {exc}") from exc  # noqa: TRY003
        if resp.status_code == _RATE_LIMITED:
            raise LLMRateLimited("ollama rate limited")  # noqa: TRY003
        if resp.status_code >= 400:  # noqa: PLR2004
            raise LLMUnavailable(f"ollama -> {resp.status_code}: {resp.text[:200]}")  # noqa: TRY003
        try:
            data = resp.json()
            content = str(data["message"]["content"])
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMUnavailable("ollama returned a malformed body") from exc  # noqa: TRY003
        return ProviderResponse(
            raw_text=content,
            resolved_model=str(data.get("model") or request.model_id),
            tokens_in=int(data.get("prompt_eval_count") or 0),
            tokens_out=int(data.get("eval_count") or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


__all__ = ["DEFAULT_BASE_URL", "OllamaProvider"]
