"""Fixture LLM provider (ARCH §9, 02 §1): deterministic answers for **code** tests.

"Fixtures test code, not prompts" (ARCH §9): this provider makes routing,
gate and outbox runs deterministic. It never judges prompt quality; that's
the real-LLM eval's job (ARCH §11). The config loader already restricts
``provider: fixture`` to ``SUPPORTAGENT_ENV=test`` or ``--demo`` (02 §1).

Answers are ordered: the first whose ``match`` occurs in the (redacted) user
prompt wins, and an empty ``match`` matches everything. An answer is either
raw text (valid JSON or deliberate garbage) or a fault (``timeout`` /
``unavailable`` / ``rate_limited``) raised as the typed domain error a real
provider would raise. With no matching answer the call fails as unavailable,
so a missing fixture fails closed (``llm-failure``), never as a guess.
The provider reports the requested model as resolved, as a pinned snapshot does.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from supportagent.domain.errors import LLMRateLimited, LLMTimeout, LLMUnavailable
from supportagent.ports.llm import ProviderRequest, ProviderResponse

Fault = Literal["timeout", "unavailable", "rate_limited"]


@dataclass(frozen=True, slots=True)
class FixtureAnswer:
    """One scripted answer; exactly one of ``raw`` / ``fault`` is set."""

    match: str = ""
    raw: str | None = None
    fault: Fault | None = None

    def __post_init__(self) -> None:
        if (self.raw is None) == (self.fault is None):
            raise ValueError("a fixture answer sets exactly one of raw / fault")  # noqa: TRY003

    @classmethod
    def triage(
        cls, classification: str, severity: str, *, match: str = "", summary: str = "fixture"
    ) -> FixtureAnswer:
        """A valid ``TriageResult`` answer."""
        body = {
            "classification": classification,
            "severity": severity,
            "summary": summary,
            "confidence": 0.9,
        }
        return cls(match=match, raw=json.dumps(body))


class FixtureLLMProvider:
    """An ``LLMProvider`` that replays scripted answers (thread-safe ``load``)."""

    def __init__(self, answers: Sequence[FixtureAnswer] = ()) -> None:
        self._lock = threading.Lock()
        self._answers = list(answers)
        self.calls = 0

    def load(self, answers: Sequence[FixtureAnswer]) -> None:
        """Replace the script (the eval harness loads one per case)."""
        with self._lock:
            self._answers = list(answers)

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        with self._lock:
            self.calls += 1
            answer = next((a for a in self._answers if a.match in request.user), None)
        if answer is None:
            raise LLMUnavailable("fixture: no scripted answer matches this request")  # noqa: TRY003
        if answer.fault == "timeout":
            raise LLMTimeout("fixture: scripted timeout")  # noqa: TRY003
        if answer.fault == "rate_limited":
            raise LLMRateLimited("fixture: scripted rate limit")  # noqa: TRY003
        if answer.fault == "unavailable":
            raise LLMUnavailable("fixture: scripted outage")  # noqa: TRY003
        raw = answer.raw or ""
        return ProviderResponse(
            raw_text=raw,
            resolved_model=request.model_id,
            tokens_in=len(request.user) // 4,
            tokens_out=len(raw) // 4,
            latency_ms=0,
        )


__all__ = ["Fault", "FixtureAnswer", "FixtureLLMProvider"]
