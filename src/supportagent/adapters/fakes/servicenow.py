"""FakeServiceNow (04 §1): an in-memory ``TicketSink`` for tests.

Serves the unit/property/invariant suites: journal entries are appended in
order, field updates are merged, and ``find_marker`` scans the journals so the
INV-04 reconciliation read is exercised against real (fake) state.

Test seams:
  * ``queue_fault(exc)`` — the next call raises ``exc`` (FIFO), used to
    simulate retryable / non-retryable external failures (01 §7.5).
  * ``journal_entries`` / ``fields`` / ``write_count`` — assertions on the
    external state (DoD: exactly one journal entry per approved action).
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping

from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.model.external import ExternalRef
from supportagent.ports.tickets import JournalField, SnTable

_JOURNAL_FIELDS: tuple[JournalField, ...] = (JournalField.WORK_NOTES, JournalField.COMMENTS)


class FakeServiceNow:
    """In-memory ticketing system (04 §1): implements ``TicketSink``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (table, sys_id, field) -> list of (entry_text, ExternalRef)
        self._journals: dict[tuple[str, str, str], list[tuple[str, ExternalRef]]] = {}
        self._fields: dict[tuple[str, str], dict[str, str]] = {}
        self._faults: deque[ExternalServiceError] = deque()
        self._jseq = 0
        self._writes = 0

    # --- test seams ----------------------------------------------------------

    def queue_fault(self, exc: ExternalServiceError) -> None:
        """Queue a fault: the next sink call raises ``exc`` (FIFO)."""
        self._faults.append(exc)

    def journal_entries(self, table: SnTable, sys_id: str, field: JournalField) -> list[str]:
        """A copy of the current entry texts of ``field`` on ``sys_id``."""
        with self._lock:
            entries = self._journals.get((table.value, sys_id, field.value), [])
            return [text for text, _ref in entries]

    def fields(self, table: SnTable, sys_id: str) -> dict[str, str]:
        """A copy of the current field values on ``sys_id``."""
        with self._lock:
            return dict(self._fields.get((table.value, sys_id), {}))

    @property
    def write_count(self) -> int:
        """Number of successful external writes (sends) so far."""
        with self._lock:
            return self._writes

    # --- TicketSink ----------------------------------------------------------

    def _maybe_fault(self) -> None:
        if self._faults:
            raise self._faults.popleft()

    def append_journal(
        self,
        table: SnTable,
        sys_id: str,
        field: JournalField,
        text: str,
    ) -> ExternalRef:
        self._maybe_fault()
        with self._lock:
            self._jseq += 1
            ref = ExternalRef(system=table.value, ref=f"snj{self._jseq:04d}")
            self._journals.setdefault((table.value, sys_id, field.value), []).append((text, ref))
            self._writes += 1
        return ref

    def update_fields(
        self,
        table: SnTable,
        sys_id: str,
        fields: Mapping[str, str],
    ) -> ExternalRef:
        self._maybe_fault()
        with self._lock:
            # read-compare: a re-apply of equal values is a no-op write (01 §7.5)
            current = self._fields.setdefault((table.value, sys_id), {})
            changed = {k: v for k, v in fields.items() if current.get(k) != v}
            if changed:
                current.update(changed)
                self._writes += 1
        return ExternalRef(system=table.value, ref=sys_id)

    def find_marker(
        self,
        table: SnTable,
        sys_id: str,
        marker: str,
    ) -> ExternalRef | None:
        with self._lock:
            for field in _JOURNAL_FIELDS:
                for text, ref in self._journals.get((table.value, sys_id, field.value), []):
                    if marker in text:
                        return ref
        return None


__all__ = ["FakeServiceNow"]
