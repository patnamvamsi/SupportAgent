"""``RetryingTicketSink`` (11-wp WP-106): bounded retries that never double-post.

A retryable ``ConnectorError`` is retried up to ``max_tries`` times. The
wait is ``Retry-After`` when the error carries it (capped at ``max_wait_s``),
else exponential backoff (0.5 s x 2^n).

What may be retried depends on whether a repeat can duplicate an effect:

- ``find_marker`` (a read) and ``update_fields`` (read-compare, idempotent):
  any retryable error.
- ``append_journal``: **only a 429**. The server rejected the request, so
  nothing was written. A 5xx or a timeout is ambiguous, because the note may
  have landed. That case goes back to the outbox executor, which checks
  the marker before re-sending (INV-04).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

from supportagent.domain.errors import ConnectorError
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import NoteVisibility, RecordType, TicketSink

T = TypeVar("T")
_RATE_LIMITED = 429


def _default_sleep(seconds: float) -> None:
    import time  # noqa: PLC0415 — deferred, like ServiceNowHTTP's

    time.sleep(seconds)


class RetryingTicketSink:
    def __init__(
        self,
        inner: TicketSink,
        *,
        max_tries: int = 3,
        backoff_base_s: float = 0.5,
        max_wait_s: float = 30.0,
        sleep: Callable[[float], None] = _default_sleep,
    ) -> None:
        self._inner = inner
        self._max_tries = max(1, max_tries)
        self._base = backoff_base_s
        self._max_wait = max_wait_s
        self._sleep = sleep

    def worst_case_s(self, call_s: float) -> float:
        """The longest one retried operation takes when a single try takes ``call_s`` (BL-001)."""
        return self._max_tries * call_s + (self._max_tries - 1) * self._max_wait

    def _retry(self, fn: Callable[[], T], *, safe: Callable[[ConnectorError], bool]) -> T:
        for attempt in range(self._max_tries):
            try:
                return fn()
            except ConnectorError as exc:
                last = attempt == self._max_tries - 1
                if last or not exc.retryable or not safe(exc):
                    raise
                wait = (
                    exc.retry_after_s
                    if exc.retry_after_s is not None
                    else (self._base * (2**attempt))
                )
                self._sleep(min(wait, self._max_wait))
        raise AssertionError("unreachable")  # pragma: no cover

    def append_journal(
        self, table: RecordType, external_id: str, field: NoteVisibility, text: str
    ) -> ExternalRef:
        return self._retry(
            lambda: self._inner.append_journal(table, external_id, field, text),
            safe=lambda exc: exc.status == _RATE_LIMITED,
        )

    def update_fields(
        self, table: RecordType, external_id: str, fields: Mapping[str, str]
    ) -> ExternalRef:
        return self._retry(
            lambda: self._inner.update_fields(table, external_id, fields), safe=lambda _: True
        )

    def find_marker(self, table: RecordType, external_id: str, marker: str) -> ExternalRef | None:
        return self._retry(
            lambda: self._inner.find_marker(table, external_id, marker), safe=lambda _: True
        )


__all__ = ["RetryingTicketSink"]
