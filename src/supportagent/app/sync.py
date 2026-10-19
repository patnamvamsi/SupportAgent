"""The sync service (ARCH §8.1): poll a ServiceNow table, dedup, filter
self-echoes, and hand results to an InboundHandler.

SyncService is bound to one tenant at construction (sync state is keyed by
(tenant_id, table)) and exposes poll_table(table) — the single
entrypoint the worker / cron calls. The service is the application boundary:
it depends only on ports (TicketSource, InboundHandler, Clock) and
the injected SyncRepo / SqlUnitOfWork (the dedup set + cursor). It
knows nothing about ServiceNow JSON, HTTP, or the database engine.

The poll loop (ARCH §8.1):

1. Read the committed cursor (or clock.now() when there is none) and subtract
   overlap_s to re-cover the boundary second (clock skew, same-second
   updates). sync_seen dedups the re-fetched records.
2. Page forward from that cursor: source.poll(table, since=cursor, limit).
   Each full page is committed (mark every processed record seen + advance the
   cursor to that page's next_cursor) *before* the next page is polled, so a
   crash mid-run resumes from the last committed cursor.
3. Per record: skip if already seen at this sys_mod_count; skip if it is a
   self-echo (sys_updated_by == integration_user — still marked seen);
   deliver as an alert on first sight, or as the new changes (via
   source.history) when a known record advances.
4. Stop on a partial page or a non-advancing cursor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import TracebackType
from typing import Protocol

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.external import ExternalCaseRef
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord
from supportagent.ports.inbound import InboundHandler
from supportagent.ports.tickets import SnTable, TicketSource


class _UoW(Protocol):
    """Structural protocol for the unit of work (the service is agnostic to the
    concrete SqlUnitOfWork implementation — it only needs __enter__ /
    __exit__ / commit)."""

    def __enter__(self) -> _UoW: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...


class _SyncRepo(Protocol):
    """Structural protocol for the sync-state repository (dedup set + cursor)."""

    def get_cursor(self, tenant_id: str, table_name: str) -> datetime | None: ...

    def set_cursor(self, tenant_id: str, table_name: str, ts: datetime) -> None: ...

    def is_seen(self, tenant_id: str, table_name: str, sys_id: str, sys_mod_count: int) -> bool: ...

    def mark_seen(
        self, tenant_id: str, table_name: str, sys_id: str, sys_mod_count: int
    ) -> None: ...

    def max_seen_mod_count(self, tenant_id: str, table_name: str, sys_id: str) -> int: ...


class _ToAlert(Protocol):
    """Structural protocol for the alert mapping (TicketRecord → AlertEvent)."""

    def __call__(
        self,
        tenant_id: str,
        rec: TicketRecord,
        *,
        received_at: datetime,
    ) -> AlertEvent: ...


class Clock(Protocol):
    """A monotonic clock seam (the service must never call datetime.now()
    directly — it is injected for testability)."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class SyncReport:
    """The result of one poll_table call (delivered count + committed cursor)."""

    delivered: int
    final_cursor: datetime


class SyncService:
    """Polls one ServiceNow table and hands results to an InboundHandler."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        tenant_id: str,
        uow: _UoW,
        repo: _SyncRepo,
        source: TicketSource,
        handler: InboundHandler,
        clock: Clock,
        integration_user: str,
        to_alert: _ToAlert,
        page_size: int = 50,
        overlap_s: int = 120,
    ) -> None:
        self._tenant_id = tenant_id
        self._uow = uow
        self._repo = repo
        self._source = source
        self._handler = handler
        self._clock = clock
        self._integration_user = integration_user
        self._to_alert = to_alert
        self._page_size = page_size
        self._overlap_s = overlap_s

    def poll_table(self, table: SnTable) -> SyncReport:
        """Poll table: dedup, filter self-echoes, deliver (ARCH §8.1).

        Pages forward from cursor - overlap_s (cursor = the committed
        cursor, or now when there is none) until a partial page or a
        non-advancing cursor. The overlap re-covers the boundary second (clock
        skew, same-second updates); sync_seen dedups the re-fetched records.
        Each full page is committed (mark seen + advance cursor) before the next
        is polled, so a crash mid-run resumes from the last committed cursor.
        The service is bound to one tenant at construction (sync state is keyed
        by (tenant_id, table)), so poll_table takes only the table.
        """
        table_name = table.value
        with self._uow:
            committed = self._repo.get_cursor(self._tenant_id, table_name)
        base = committed if committed is not None else self._clock.now()
        cursor = base - timedelta(seconds=self._overlap_s)
        # The cursor actually committed to the DB (starts at the poll start; each
        # committed page advances it to that page's next_cursor). The result
        # must report what was committed, not the pre-loop poll start.
        final_cursor = cursor

        delivered = 0
        while True:
            # External poll — no DB lock held.
            batch = self._source.poll(table, since=cursor, limit=self._page_size)

            # Read block: dedup + echo decisions (read-only, rolled back).
            decisions: list[tuple[TicketRecord, bool, int]] = []
            with self._uow:
                for rec in batch.records:
                    if self._repo.is_seen(
                        self._tenant_id, table_name, rec.sys_id, rec.sys_mod_count
                    ):
                        continue  # this exact version was already delivered
                    is_echo = rec.sys_updated_by == self._integration_user
                    after = self._repo.max_seen_mod_count(self._tenant_id, table_name, rec.sys_id)
                    decisions.append((rec, is_echo, after))

            # External history + handler delivery — no DB lock held.
            for rec, is_echo, after in decisions:
                if is_echo:
                    continue  # self-echo: not delivered (still marked seen below)
                if after == 0:
                    # First sight of this record: deliver as an alert.
                    self._handler.on_alert(
                        self._to_alert(self._tenant_id, rec, received_at=self._clock.now())
                    )
                else:
                    # Known record, new version: deliver the new changes.
                    changes: Sequence[ChangeRecord] = self._source.history(
                        table, rec.sys_id, after_mod_count=after
                    )
                    self._handler.on_changes(
                        ExternalCaseRef(table=rec.table, sys_id=rec.sys_id, number=rec.number),
                        changes,
                    )
                delivered += 1

            # Write block: mark every processed record seen + advance the cursor.
            with self._uow as uow:
                for rec, _, _ in decisions:
                    self._repo.mark_seen(self._tenant_id, table_name, rec.sys_id, rec.sys_mod_count)
                self._repo.set_cursor(self._tenant_id, table_name, batch.next_cursor)
                uow.commit()
                final_cursor = batch.next_cursor

            # Stop on a partial page (no more records) or a non-advancing cursor.
            if len(batch.records) < self._page_size:
                break
            if batch.next_cursor <= cursor:
                break
            cursor = batch.next_cursor

        return SyncReport(delivered=delivered, final_cursor=final_cursor)
