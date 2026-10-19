"""WP-008: the ``SyncService`` behaviour (10-wp L128-132; ARCH §8.1).

Pins the shared behaviour of the sync service — first-sight alerts, dedup on
``(sys_id, sys_mod_count)``, self-echo filtering, per-page cursor advancement,
and crash-mid-batch recovery — against the in-memory ``FakeServiceNow`` source
and the real ``SyncRepo`` (the dedup set + per-(tenant, table) cursor over the
``sync_seen`` / ``sync_cursor`` tables). The assertions are on the service's
*observable behaviour* (delivered count, committed cursor, which record
versions were delivered), not on any per-impl detail — so the same test pins
the contract regardless of which ``TicketSource`` / ``SyncRepo`` is wired.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from supportagent.adapters.db.repos.sync import SyncRepo
from supportagent.adapters.servicenow.mapping import record_to_alert
from supportagent.app.sync import SyncService
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.external import ExternalCaseRef
from supportagent.domain.model.ticket import ChangeRecord
from supportagent.ports.tickets import SnTable
from tests.builders import T0

TENANT = "acme"
TABLE = SnTable.INCIDENT
TABLE_NAME = "incident"
INTEGRATION_USER = "svc_supportagent"
PAGE_SIZE = 2
OVERLAP_S = 120


@dataclass
class _RecordingHandler:
    """An ``InboundHandler`` that records alerts + changes for assertions."""

    alerts: list[AlertEvent] = field(default_factory=list)
    changes: list[tuple[ExternalCaseRef, Sequence[ChangeRecord]]] = field(default_factory=list)

    def on_alert(self, evt: AlertEvent) -> None:
        self.alerts.append(evt)

    def on_changes(self, case_ref: ExternalCaseRef, changes: Sequence[ChangeRecord]) -> None:
        self.changes.append((case_ref, list(changes)))


def _seed(fake_sn: Any, sys_id: str, number: str, at: Any, *, user: str = "u1") -> None:
    """Seed one incident record (a distinct number — required when origin is
    ``"ticket"`` — the ``AlertEvent`` validator enforces)."""
    fake_sn.seed_record(
        TABLE,
        sys_id,
        number=number,
        short_description=f"desc {sys_id}",
        priority="3",
        state="1",
        sys_updated_on=at,
        sys_updated_by=user,
        sys_mod_count=1,
    )


def _service(uow: Any, fake_sn: Any, handler: _RecordingHandler, clock: Any) -> SyncService:
    """Wire the sync service over the in-memory source + real ``SyncRepo``."""
    return SyncService(
        tenant_id=TENANT,
        uow=uow,
        repo=SyncRepo(uow),
        source=fake_sn,
        handler=handler,
        clock=clock,
        integration_user=INTEGRATION_USER,
        to_alert=record_to_alert,
        page_size=PAGE_SIZE,
        overlap_s=OVERLAP_S,
    )


def test_sync_same_second_not_missed(uow: Any, clock: Any, fake_sn: Any) -> None:
    """A record updated in the *same second* as the committed cursor is not
    missed (the inclusive ``>=`` window + the overlap re-cover the boundary)."""
    handler = _RecordingHandler()
    service = _service(uow, fake_sn, handler, clock)

    _seed(fake_sn, "id_a", "INC1", T0)
    report = service.poll_table(TABLE)
    assert report.delivered == 1
    assert report.final_cursor == T0
    assert {a.external_id for a in handler.alerts} == {"id_a"}

    # a second record lands in the same second as the committed cursor
    _seed(fake_sn, "id_b", "INC2", T0)
    service.poll_table(TABLE)
    assert {a.external_id for a in handler.alerts} == {"id_a", "id_b"}


def test_sync_seen_prevents_reprocessing(uow: Any, clock: Any, fake_sn: Any) -> None:
    """A record already delivered is not delivered again, even when the
    overlap window re-fetches it (``sync_seen`` dedups on ``(sys_id, mod_count)``)."""
    handler = _RecordingHandler()
    service = _service(uow, fake_sn, handler, clock)

    _seed(fake_sn, "id_a", "INC1", T0)
    assert service.poll_table(TABLE).delivered == 1
    assert len(handler.alerts) == 1

    # re-poll: the same record is re-fetched across the overlap, but deduped
    assert service.poll_table(TABLE).delivered == 0
    assert len(handler.alerts) == 1
    assert handler.changes == []


def test_sync_echo_filter_skips_integration_user(uow: Any, clock: Any, fake_sn: Any) -> None:
    """A record we updated (``sys_updated_by == integration_user``) is not
    re-ingested, but is still marked seen and advances the cursor."""
    handler = _RecordingHandler()
    service = _service(uow, fake_sn, handler, clock)

    _seed(fake_sn, "id_a", "INC1", T0, user=INTEGRATION_USER)
    report = service.poll_table(TABLE)
    assert report.delivered == 0
    assert handler.alerts == []
    assert handler.changes == []
    # the echo was still marked seen and advanced the committed cursor
    repo = SyncRepo(uow)
    with uow:
        assert repo.get_cursor(TENANT, TABLE_NAME) == T0
        assert repo.is_seen(TENANT, TABLE_NAME, "id_a", 1)


def test_sync_cursor_advances_per_page(uow: Any, clock: Any, fake_sn: Any) -> None:
    """The cursor advances per page (``batch.next_cursor``), and one
    ``poll_table`` call pages through every changed record exactly once."""
    handler = _RecordingHandler()
    service = _service(uow, fake_sn, handler, clock)

    _seed(fake_sn, "id_a", "INC1", T0)
    _seed(fake_sn, "id_b", "INC2", T0 + timedelta(seconds=1))
    _seed(fake_sn, "id_c", "INC3", T0 + timedelta(seconds=2))
    report = service.poll_table(TABLE)
    assert report.delivered == 3
    assert report.final_cursor == T0 + timedelta(seconds=2)
    assert {a.external_id for a in handler.alerts} == {"id_a", "id_b", "id_c"}
    # the committed cursor is the last page's ``next_cursor``
    repo = SyncRepo(uow)
    with uow:
        assert repo.get_cursor(TENANT, TABLE_NAME) == T0 + timedelta(seconds=2)


def test_sync_crash_mid_batch_recovers(uow: Any, clock: Any, fake_sn: Any) -> None:
    """A crash mid-run leaves the last committed cursor intact; the next run
    re-polls from it, dedups the re-fetched page, and delivers the rest once
    (at-least-once, no loss, no double-delivery of the same version)."""
    handler = _RecordingHandler()
    service = _service(uow, fake_sn, handler, clock)

    for i in range(5):
        _seed(fake_sn, f"id_{chr(ord('a') + i)}", f"INC{i + 1}", T0 + timedelta(seconds=i))

    # Simulate a crash after page 1 (``page_size=2``): page 1 committed id_a +
    # id_b as seen and advanced the cursor to T0+1s, then the worker died.
    repo = SyncRepo(uow)
    with uow:
        repo.mark_seen(TENANT, TABLE_NAME, "id_a", 1)
        repo.mark_seen(TENANT, TABLE_NAME, "id_b", 1)
        repo.set_cursor(TENANT, TABLE_NAME, T0 + timedelta(seconds=1))
        uow.commit()

    # Recovery: re-polls from the committed cursor, dedups the re-fetched page,
    # and delivers the remaining three exactly once.
    report = service.poll_table(TABLE)
    assert report.delivered == 3
    assert {a.external_id for a in handler.alerts} == {"id_c", "id_d", "id_e"}
