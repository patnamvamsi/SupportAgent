"""WP-007: ``LLMClient.structured`` — redact, call, parse, never raise (01 §6).

Canonical test IDs (10-wp WP-007):

- ``test_structured_returns_none_on_schema_invalid``
- ``test_structured_returns_none_on_timeout``
- ``test_structured_records_rendered_prompt_hash``

``structured()`` is the single LLM boundary: it renders the slot prompt,
redacts system+user, calls the provider, records a ``redact`` then an ``llm``
event, and returns a validated domain model or ``None`` — it never raises to
the caller.
"""

from __future__ import annotations

from typing import Any

from supportagent.domain.errors import LLMTimeout
from supportagent.domain.events import EventKind
from supportagent.domain.model.triage import TriageResult
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext

_TRACE = TraceContext.new("acme", "trace_0001", "span_llm")
_VARS: dict[str, str] = {
    "ticket_subject": "DNS failure",
    "ticket_body": "Users cannot resolve hostnames since 02:00 UTC.",
}


def test_structured_returns_none_on_schema_invalid(
    structured_llm: Any, fake_llm: Any, events: Any
) -> None:
    """A schema-invalid JSON body is a failed call (``None``), never an error."""
    # Valid shape, but missing the required ``confidence`` field.
    fake_llm.respond('{"classification": "network/dns", "severity": "P2", "summary": "x"}')
    result = structured_llm.structured(SlotName.CLASSIFY, dict(_VARS), TriageResult, trace=_TRACE)
    assert result is None
    # The call is still recorded — as an ``llm`` event with status ``error``.
    llm_events = [event for event in events.appended if event.kind == EventKind.LLM]
    assert llm_events, "no llm event recorded for a failed call"
    assert llm_events[0].status == "error"


def test_structured_returns_none_on_timeout(
    structured_llm: Any, fake_llm: Any, events: Any
) -> None:
    """A provider timeout (``LLMTimeout``) is swallowed into ``None`` + error."""
    fake_llm.fail_with(LLMTimeout("timed out"))
    result = structured_llm.structured(SlotName.CLASSIFY, dict(_VARS), TriageResult, trace=_TRACE)
    assert result is None
    llm_events = [event for event in events.appended if event.kind == EventKind.LLM]
    assert llm_events and llm_events[0].status == "error"
    # Redaction always happens before the call, so a redact event is recorded.
    assert EventKind.REDACT in {event.kind for event in events.appended}


def test_structured_records_rendered_prompt_hash(structured_llm: Any, events: Any) -> None:
    """The ``llm`` event records a deterministic 64-hex ``rendered_prompt_hash``."""
    for _ in range(2):
        structured_llm.structured(SlotName.CLASSIFY, dict(_VARS), TriageResult, trace=_TRACE)
    llm_events = [event for event in events.appended if event.kind == EventKind.LLM]
    assert len(llm_events) == 2
    hashes = {event.payload["rendered_prompt_hash"] for event in llm_events}
    assert len(hashes) == 1, "rendered_prompt_hash must be deterministic for the same input"
    digest = next(iter(hashes))
    assert isinstance(digest, str) and len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
