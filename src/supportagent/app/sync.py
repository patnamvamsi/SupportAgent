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
2. Page through ``sys_updated_on >= since`` by offset (02 §6
   ``sysparm_offset``) from that fixed ``since``, so a burst of more than
   ``page_size`` records in one second cannot stall the cursor. Each page is
   committed (mark seen + advance the cursor to the page's max
   ``sys_updated_on``) before the next is polled; a crash resumes from the
   last committed cursor.
3. Per record not yet seen at this sys_mod_count:
   - first sight: deliver as an alert, unless the integration user wrote it
     (a record we created is not an inbound alert);
   - known record: read its history since the last processed mod_count and
     drop changes whose actor is the integration user — **per change**, never
     on the record's last ``sys_updated_by``, so a human change followed by
     our own work note in the same interval is still delivered.
4. Stop on a partial page.
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

    def is_seen(self, tenant_id: str, sys_id: str, sys_mod_count: int) -> bool: ...

    def mark_seen(
        self, tenant_id: str, sys_id: str, sys_mod_count: int, *, now_ts: int
    ) -> None: ...

    def max_seen_mod_count(self, tenant_id: str, sys_id: str) -> int | None: ...


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
        """Poll ``table``: dedup, filter self-echoes per change, deliver (ARCH §8.1).

        Pages by offset through everything updated since ``cursor - overlap_s``
        (cursor = the committed cursor, or now when there is none) until a
        partial page. Each page commits its ``sync_seen`` rows and the cursor
        before the next is polled. The service is bound to one tenant at
        construction, so ``poll_table`` takes only the table.
        """
        table_name = table.value
        with self._uow:
            committed = self._repo.get_cursor(self._tenant_id, table_name)
        base = committed if committed is not None else self._clock.now()
        since = base - timedelta(seconds=self._overlap_s)
        final_cursor = committed if committed is not None else since

        delivered = 0
        offset = 0
        while True:
            # External poll — no DB transaction held.
            batch = self._source.poll(table, since=since, limit=self._page_size, offset=offset)

            # Read block: which versions are new, and what we last processed.
            pending: list[tuple[TicketRecord, int | None]] = []
            with self._uow:
                for rec in batch.records:
                    if self._repo.is_seen(self._tenant_id, rec.sys_id, rec.sys_mod_count):
                        continue  # this exact version was already processed
                    after = self._repo.max_seen_mod_count(self._tenant_id, rec.sys_id)
                    pending.append((rec, after))

            # History + delivery — no DB transaction held.
            for rec, after in pending:
                if self._deliver(table, rec, after, since):
                    delivered += 1

            # Write block: mark the page processed + advance the cursor.
            with self._uow as uow:
                now_ts = int(self._clock.now().timestamp())
                for rec, _ in pending:
                    self._repo.mark_seen(
                        self._tenant_id, rec.sys_id, rec.sys_mod_count, now_ts=now_ts
                    )
                if batch.records:
                    self._repo.set_cursor(self._tenant_id, table_name, batch.next_cursor)
                    final_cursor = max(final_cursor, batch.next_cursor)
                uow.commit()

            if len(batch.records) < self._page_size:
                break
            offset += self._page_size

        return SyncReport(delivered=delivered, final_cursor=final_cursor)

    def _deliver(
        self, table: SnTable, rec: TicketRecord, after: int | None, since: datetime
    ) -> bool:
        """Hand one new record version to the handler; ``True`` if delivered."""
        if after is None:
            # First sight. A record the integration user wrote is our own.
            if rec.sys_updated_by == self._integration_user:
                return False
            self._handler.on_alert(
                self._to_alert(self._tenant_id, rec, received_at=self._clock.now())
            )
            return True
        # Known record: the per-change history, minus our own changes.
        changes: Sequence[ChangeRecord] = [
            c
            for c in self._source.history(table, rec.sys_id, after_mod_count=after, since=since)
            if c.user != self._integration_user
        ]
        if not changes:
            return False  # only our own writes (self-echo)
        self._handler.on_changes(
            ExternalCaseRef(table=rec.table, sys_id=rec.sys_id, number=rec.number), changes
        )
        return True
