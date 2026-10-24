"""WP-007: fault injection at the LLM boundary (04 §1, INV-05/INV-06).

Canonical test ID (10-wp WP-007):

- ``test_fault_injection_llm_timeout_yields_none``

``SUPPORTAGENT_FAULT_INJECT=llm_timeout`` makes the provider fail; the client
must swallow it into ``None`` (never raise) and record an ``llm`` event with
status ``error``. The inner provider is never reached — the fault
short-circuits before any network call.

This test is deliberately marked ``unit`` (lives under ``tests/unit/``) so
``make test`` (``unit or property``) covers the fault path without a separate
``make faults`` target.
"""

from __future__ import annotations

from typing import Any

import pytest

from supportagent.domain.events import EventKind
from supportagent.domain.model.triage import TriageResult
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext

_TRACE = TraceContext.new("acme", "trace_0001", "span_llm")
_VARS: dict[str, str] = {
    "taxonomy_labels": "- database/saturation: pool saturation",
    "ticket_subject": "DNS failure",
    "ticket_body": "Users cannot resolve hostnames since 02:00 UTC.",
}


@pytest.mark.faults
def test_fault_injection_llm_timeout_yields_none(
    monkeypatch: pytest.MonkeyPatch,
    fake_llm: Any,
    events: Any,
    _build_llm_client: Any,
) -> None:
    """``llm_timeout`` fault → ``None`` + error event; inner provider untouched."""
    monkeypatch.setenv("SUPPORTAGENT_FAULT_INJECT", "llm_timeout")
    from supportagent.adapters.llm.faults import FaultInjectingLLMProvider  # noqa: PLC0415

    client = _build_llm_client(FaultInjectingLLMProvider(fake_llm))
    result = client.structured(SlotName.CLASSIFY, dict(_VARS), TriageResult, trace=_TRACE)

    assert result is None, "a faulted call must return None, never raise"
    # The fault short-circuits the inner provider — no request was captured.
    assert fake_llm.captured == [], "the fault must not reach the inner provider"
    llm_events = [event for event in events.appended if event.kind == EventKind.LLM]
    assert llm_events and llm_events[0].status == "error"
    # Redaction always precedes the call.
    assert EventKind.REDACT in {event.kind for event in events.appended}


@pytest.mark.faults
@pytest.mark.parametrize("fault", ["llm_garbage", "llm_429"])
def test_fault_injection_spec_faults_yield_none(
    monkeypatch: pytest.MonkeyPatch,
    fake_llm: Any,
    events: Any,
    _build_llm_client: Any,
    fault: str,
) -> None:
    """10-wp WP-007 fault names: garbage payload and 429 both collapse to ``None``."""
    monkeypatch.setenv("SUPPORTAGENT_FAULT_INJECT", fault)
    from supportagent.adapters.llm.faults import FaultInjectingLLMProvider  # noqa: PLC0415

    client = _build_llm_client(FaultInjectingLLMProvider(fake_llm))
    result = client.structured(SlotName.CLASSIFY, dict(_VARS), TriageResult, trace=_TRACE)

    assert result is None
    assert fake_llm.captured == []
    llm_events = [event for event in events.appended if event.kind == EventKind.LLM]
    assert llm_events and llm_events[0].status == "error"
