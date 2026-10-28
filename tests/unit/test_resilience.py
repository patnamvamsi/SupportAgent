"""WP-106: retry, circuit breaking, degraded mode, model deprecation (ARCH §2, §9).

Canonical test IDs from 11-wp §WP-106 that live here:
``test_breaker_opens_and_half_opens``, ``test_retry_respects_retry_after``,
``test_degraded_flag_on_events_when_llm_breaker_open``,
``test_deprecation_warning_at_t_minus_90``, ``test_retired_model_slot_safe_defaults``.
``test_fault_injection_suite`` (the full matrix) is in
``tests/integration/test_fault_suite.py``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from supportagent.adapters.files.deprecation_loader import load_deprecation_feed
from supportagent.adapters.llm.retirement import RetiredModelGuard
from supportagent.adapters.resilience.breaker import (
    CircuitBreaker,
    CircuitBreakerLLMProvider,
    CircuitBreakerTicketSink,
)
from supportagent.adapters.resilience.retry import RetryingTicketSink
from supportagent.adapters.servicenow.errors import ServiceNowError
from supportagent.config.loader import load_fault_injection
from supportagent.domain.deprecation import deprecation_notices
from supportagent.domain.errors import ConfigError, ConnectorError, LLMUnavailable
from supportagent.domain.events import EventKind
from supportagent.domain.faults import Fault, parse_faults
from supportagent.domain.model.external import ExternalRef
from supportagent.domain.model.triage import SafeDefaulted, TriageResult
from supportagent.domain.slots import SlotName
from supportagent.domain.trace import TraceContext
from supportagent.entrypoints.worker.jobs import llm_deprecation_check
from supportagent.ports.tickets import NoteVisibility, RecordType
from tests.unit.test_triage_service import _evt, _wire

_REF = ExternalRef(system="servicenow", ref="sys_journal/1")
_MODEL = "claude-sonnet-4-5-20250929"


class _FlakySink:
    """Fails with the queued errors, then succeeds; counts calls per method."""

    def __init__(self, errors: list[ServiceNowError] | None = None) -> None:
        self.errors = list(errors or [])
        self.calls: dict[str, int] = {}

    def _call(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.errors:
            raise self.errors.pop(0)

    def append_journal(self, table: Any, external_id: str, field: Any, text: str) -> ExternalRef:
        self._call("append_journal")
        return _REF

    def update_fields(self, table: Any, external_id: str, fields: Any) -> ExternalRef:
        self._call("update_fields")
        return _REF

    def find_marker(self, table: Any, external_id: str, marker: str) -> ExternalRef | None:
        self._call("find_marker")
        return None


def _sn500() -> ServiceNowError:
    return ServiceNowError("500", retryable=True, status=500)


# --- breaker -----------------------------------------------------------------


def _state(breaker: CircuitBreaker) -> str:
    """Read through a call, so mypy doesn't narrow the property across clock moves."""
    return breaker.state


def test_breaker_opens_and_half_opens(clock: Any) -> None:
    inner = _FlakySink([_sn500() for _ in range(5)])
    breaker = CircuitBreaker(clock=clock, threshold=5, open_s=60)
    sink = CircuitBreakerTicketSink(inner, breaker)
    for _ in range(5):
        with pytest.raises(ServiceNowError, match="500"):
            sink.find_marker(RecordType.INCIDENT, "s", "m")
    assert _state(breaker) == "open"
    with pytest.raises(ConnectorError, match="circuit open") as refused:
        sink.find_marker(RecordType.INCIDENT, "s", "m")
    assert refused.value.retryable and refused.value.retry_after_s == pytest.approx(60)
    assert inner.calls["find_marker"] == 5, "an open breaker never calls through"

    clock.advance(60)
    assert _state(breaker) == "half_open"
    inner.errors = [_sn500()]
    with pytest.raises(ServiceNowError, match="500"):
        sink.find_marker(RecordType.INCIDENT, "s", "m")  # the one probe fails
    assert _state(breaker) == "open", "a failed probe re-opens at once"

    clock.advance(60)
    assert sink.find_marker(RecordType.INCIDENT, "s", "m") is None  # the probe succeeds
    assert _state(breaker) == "closed"


def test_breaker_ignores_non_infrastructure_errors(clock: Any) -> None:
    breaker = CircuitBreaker(clock=clock, threshold=1)

    def bad() -> None:
        raise ValueError("bad data")  # noqa: TRY003

    with pytest.raises(ValueError, match="bad data"):
        breaker.call(bad)
    assert breaker.state == "closed"


# --- retry -------------------------------------------------------------------


def test_retry_respects_retry_after() -> None:
    slept: list[float] = []
    limited = ServiceNowError("429", retryable=True, retry_after_s=7, status=429)
    inner = _FlakySink([limited])
    sink = RetryingTicketSink(inner, sleep=slept.append)
    sink.append_journal(RecordType.INCIDENT, "s", NoteVisibility.INTERNAL, "t")
    assert slept == [7] and inner.calls["append_journal"] == 2
    # Retry-After is capped; without it the backoff is exponential.
    inner.errors = [ServiceNowError("429", retryable=True, retry_after_s=999, status=429)]
    sink.append_journal(RecordType.INCIDENT, "s", NoteVisibility.INTERNAL, "t")
    inner.errors = [_sn500(), _sn500()]
    sink.find_marker(RecordType.INCIDENT, "s", "m")
    assert slept == [7, 30.0, 0.5, 1.0]


def test_ambiguous_journal_failure_is_not_retried() -> None:
    """A 5xx on an append may have landed: the executor reconciles, not the sink (INV-04)."""
    inner = _FlakySink([_sn500()])
    sink = RetryingTicketSink(inner, sleep=lambda _: None)
    with pytest.raises(ServiceNowError):
        sink.append_journal(RecordType.INCIDENT, "s", NoteVisibility.INTERNAL, "t")
    assert inner.calls["append_journal"] == 1
    inner.errors = [ServiceNowError("400", retryable=False, status=400)]
    with pytest.raises(ServiceNowError):
        sink.update_fields(RecordType.INCIDENT, "s", {"a": "b"})
    assert inner.calls["update_fields"] == 1, "non-retryable errors are never retried"


# --- degraded mode -----------------------------------------------------------


class _DownLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, request: Any) -> Any:
        self.calls += 1
        raise LLMUnavailable("provider down")  # noqa: TRY003


def test_degraded_flag_on_events_when_llm_breaker_open(
    structured_llm: Any, events: Any, clock: Any
) -> None:
    down = _DownLLM()
    breaker = CircuitBreaker(clock=clock, threshold=5)
    structured_llm._provider = CircuitBreakerLLMProvider(down, breaker)
    trace = TraceContext.new("acme", "trace_1", "span_1")
    variables = {
        "taxonomy_labels": "- database/saturation: pool saturation",
        "pack_guidance": "",
        "ticket_subject": "s",
        "ticket_body": "b",
    }
    for _ in range(6):
        assert structured_llm.structured(SlotName.CLASSIFY, variables, TriageResult, trace) is None
    assert down.calls == 5, "the sixth call never reached the provider"
    llm = [e for e in events.appended if e.kind is EventKind.LLM]
    assert [e.payload.get("degraded", False) for e in llm] == [False] * 5 + [True]
    assert llm[-1].payload["error"] == "LLMCircuitOpen"


# --- deprecation --------------------------------------------------------------


def test_deprecation_warning_at_t_minus_90(caplog: pytest.LogCaptureFixture) -> None:
    today = date(2026, 10, 21)
    slots = {"classify": "m-a", "entail": "m-b", "draft": "m-c", "other": "m-d"}
    feed = {
        "m-a": today + timedelta(days=91),  # not yet
        "m-b": today + timedelta(days=90),  # T-90: warn
        "m-c": today,  # retired today
    }
    notices = deprecation_notices(slots, feed, today)
    assert [(n.slot, n.status, n.days_left) for n in notices] == [
        ("draft", "retired", 0),
        ("entail", "warn", 90),
    ]

    class _Lease:
        def acquire(self, **_: Any) -> bool:
            return True

        def release(self, **_: Any) -> None:
            return None

    with caplog.at_level(logging.WARNING, logger="supportagent.worker"):
        res = llm_deprecation_check(notices=lambda: notices, lease=_Lease(), owner="w")
    assert res.ran and res.rows == 2
    assert "entail" in caplog.text and "retired" in caplog.text


def test_deprecation_feed_loader(tmp_path: Path) -> None:
    path = tmp_path / "feed.yaml"
    path.write_text(f"models:\n  {_MODEL}: {{retires_on: 2027-09-29}}\n", encoding="utf-8")
    assert load_deprecation_feed(path) == {_MODEL: date(2027, 9, 29)}
    path.write_text("models:\n  m: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="retires_on"):
        load_deprecation_feed(path)


def test_retired_model_slot_safe_defaults(  # noqa: PLR0913, PLR0917
    uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any, db: Any
) -> None:
    feed = {_MODEL: clock.now().date() - timedelta(days=1)}
    guarded = RetiredModelGuard(fake_llm, feed=feed, clock=clock)
    w = _wire(uow, clock, ids, aead, guarded)
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, SafeDefaulted)
    assert outcome.decision.reason == "llm-failure"
    assert fake_llm.captured == [], "a retired model is never called"


# --- fault parsing -----------------------------------------------------------


def test_fault_names_parse_and_unknown_is_rejected() -> None:
    assert parse_faults("llm_timeout, sn_500") == {Fault.LLM_TIMEOUT, Fault.SN_500}
    assert parse_faults(None) == frozenset()
    with pytest.raises(ConfigError, match="llm_slow"):
        parse_faults("llm_slow")
    assert load_fault_injection({"SUPPORTAGENT_FAULT_INJECT": "crash_after_send"}) == {
        Fault.CRASH_AFTER_SEND
    }
