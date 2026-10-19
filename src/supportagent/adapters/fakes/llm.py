"""In-memory LLM provider fake (01 §3, 04 §1).

``FakeLLMProvider`` is the test double for the ``LLMProvider`` port: it
captures every request it is given, returns a canned response (a valid
``TriageResult`` JSON by default), and can be told to fail with a domain error.
It imports only the ``ports`` and ``domain`` layers — never ``adapters.llm``
(``adapters-independent`` contract) — so it is a genuine black-box double.
"""

from __future__ import annotations

from supportagent.ports.llm import ProviderRequest, ProviderResponse

# A valid ``TriageResult`` JSON body (01 §4): classification, severity,
# summary, confidence. Used as the default canned response.
_DEFAULT_RESPONSE = (
    '{"classification": "network/dns", "severity": "P2", "summary": "x", "confidence": 0.9}'
)


class FakeLLMProvider:
    """Captures requests and returns a canned response (or a configured fault)."""

    def __init__(self) -> None:
        self.captured: list[ProviderRequest] = []
        self._response: str = _DEFAULT_RESPONSE
        self._fail_with: Exception | None = None

    def respond(self, json_str: str) -> None:
        """Set the canned JSON response body for the next call."""
        self._response = json_str

    def fail_with(self, exc: Exception) -> None:
        """Make the next call raise ``exc`` (a domain error)."""
        self._fail_with = exc

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        self.captured.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return ProviderResponse(
            raw_text=self._response,
            resolved_model="fake-model",
            tokens_in=10,
            tokens_out=20,
            latency_ms=12,
        )
