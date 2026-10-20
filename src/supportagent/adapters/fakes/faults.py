"""Fault-injecting sink (11-wp WP-106, 04 §1): ``sn_500``, ``sn_429``, ``crash_after_send``.

``FaultInjectingTicketSink`` wraps any ``TicketSink``:

- ``sn_500``: every call raises a retryable ``ServiceNowError`` (status 500)
  before reaching the inner sink.
- ``sn_429``: the same with status 429 and ``Retry-After`` 1 s.
- ``crash_after_send``: the write **reaches** the inner sink, and then the
  process "dies" (``SimulatedCrash``, a ``BaseException`` no handler
  catches). This is the INV-04 window: sent but not recorded. The next
  executor run must reconcile through the marker and not send again.
  ``crash_once`` (the default) dies only on the first write.

The LLM faults live in ``adapters/llm/faults.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from supportagent.domain.errors import ServiceNowError
from supportagent.domain.faults import Fault
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import JournalField, SnTable, TicketSink


class SimulatedCrash(BaseException):
    """The process died between the external write and the outbox outcome."""


class FaultInjectingTicketSink:
    def __init__(
        self, inner: TicketSink, faults: Collection[Fault], *, crash_once: bool = True
    ) -> None:
        self._inner = inner
        self._faults = frozenset(faults)
        self._crash_once = crash_once
        self.crashes = 0

    def _before(self) -> None:
        if Fault.SN_500 in self._faults:
            raise ServiceNowError("injected fault: sn_500", retryable=True, status=500)  # noqa: TRY003
        if Fault.SN_429 in self._faults:
            raise ServiceNowError(  # noqa: TRY003
                "injected fault: sn_429", retryable=True, retry_after_s=1.0, status=429
            )

    def _after_write(self) -> None:
        if Fault.CRASH_AFTER_SEND in self._faults and not (self._crash_once and self.crashes):
            self.crashes += 1
            raise SimulatedCrash("injected fault: crash_after_send")  # noqa: TRY003

    def append_journal(
        self, table: SnTable, sys_id: str, field: JournalField, text: str
    ) -> ExternalRef:
        self._before()
        ref = self._inner.append_journal(table, sys_id, field, text)
        self._after_write()
        return ref

    def update_fields(self, table: SnTable, sys_id: str, fields: Mapping[str, str]) -> ExternalRef:
        self._before()
        ref = self._inner.update_fields(table, sys_id, fields)
        self._after_write()
        return ref

    def find_marker(self, table: SnTable, sys_id: str, marker: str) -> ExternalRef | None:
        self._before()
        return self._inner.find_marker(table, sys_id, marker)


__all__ = ["FaultInjectingTicketSink", "SimulatedCrash"]
