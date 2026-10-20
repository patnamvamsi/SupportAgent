"""Circuit breaking (11-wp WP-106, ARCH §2 INV-05).

``CircuitBreaker`` opens after ``threshold`` (5) consecutive failures and
stays open for ``open_s`` (60 s). Then it half-opens: one probe call goes
through, and a success closes it while a failure re-opens it for another
``open_s``. Calls made while it's open fail at once, without touching the
dependency:

- ``CircuitBreakerTicketSink`` raises a retryable ``ServiceNowError`` whose
  ``retry_after_s`` is the time left. The outbox backs off and reconciles as
  for any other failure.
- ``CircuitBreakerLLMProvider`` raises ``LLMCircuitOpen``. ``LLMClient`` turns
  that into ``None``, so triage safe-defaults: **degraded mode**, and the
  ``llm`` event carries ``degraded: true``.

Only infrastructure errors (``ExternalServiceError``) count as failures; a
bad request or bad data is not the dependency being down.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Literal, TypeVar

from supportagent.domain.errors import ExternalServiceError, LLMCircuitOpen, ServiceNowError
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.clock import Clock
from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse
from supportagent.ports.tickets import JournalField, SnTable, TicketSink

BreakerState = Literal["closed", "open", "half_open"]
T = TypeVar("T")

DEFAULT_THRESHOLD = 5
DEFAULT_OPEN_S = 60.0


class CircuitOpenError(Exception):
    """Internal: the breaker refused the call (callers translate it)."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"circuit open; retry in {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


class CircuitBreaker:
    def __init__(
        self,
        *,
        clock: Clock,
        threshold: int = DEFAULT_THRESHOLD,
        open_s: float = DEFAULT_OPEN_S,
        name: str = "breaker",
    ) -> None:
        self._clock = clock
        self._threshold = threshold
        self._open_s = open_s
        self.name = name
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._now() - self._opened_at >= self._open_s:
            return "half_open"
        return "open"

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            state = self._state()
            if state == "open" or (state == "half_open" and self._probing):
                opened = self._opened_at if self._opened_at is not None else self._now()
                raise CircuitOpenError(max(0.0, self._open_s - (self._now() - opened)))
            if state == "half_open":
                self._probing = True
        try:
            result = fn()
        except ExternalServiceError:
            self._record(success=False)
            raise
        self._record(success=True)
        return result

    def _record(self, *, success: bool) -> None:
        with self._lock:
            probing, self._probing = self._probing, False
            if success:
                self._failures = 0
                self._opened_at = None
                return
            self._failures += 1
            if probing or self._failures >= self._threshold:
                self._opened_at = self._now()


class CircuitBreakerTicketSink:
    """A ``TicketSink`` that fails fast while ServiceNow is down."""

    def __init__(self, inner: TicketSink, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def _guard(self, fn: Callable[[], T]) -> T:
        try:
            return self._breaker.call(fn)
        except CircuitOpenError as exc:
            raise ServiceNowError(  # noqa: TRY003
                f"ServiceNow circuit open ({self._breaker.name})",
                retryable=True,
                retry_after_s=exc.retry_after_s,
            ) from exc

    def append_journal(
        self, table: SnTable, sys_id: str, field: JournalField, text: str
    ) -> ExternalRef:
        return self._guard(lambda: self._inner.append_journal(table, sys_id, field, text))

    def update_fields(self, table: SnTable, sys_id: str, fields: Mapping[str, str]) -> ExternalRef:
        return self._guard(lambda: self._inner.update_fields(table, sys_id, fields))

    def find_marker(self, table: SnTable, sys_id: str, marker: str) -> ExternalRef | None:
        return self._guard(lambda: self._inner.find_marker(table, sys_id, marker))


class CircuitBreakerLLMProvider:
    """An ``LLMProvider`` that stops calling a failing provider (degraded mode)."""

    def __init__(self, inner: LLMProvider, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        try:
            return self._breaker.call(lambda: self._inner.complete_json(request))
        except CircuitOpenError as exc:
            raise LLMCircuitOpen(  # noqa: TRY003
                f"LLM circuit open; retry in {exc.retry_after_s:.0f}s"
            ) from exc


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitBreakerLLMProvider",
    "CircuitBreakerTicketSink",
]
