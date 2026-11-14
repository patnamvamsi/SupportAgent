"""WP-006: the ``outbox.execute`` worker job (02 §7, 10-wp).

Pins the thin entrypoint contract: the job takes a DB lease (03 §2.5) so at
most one replica drains the outbox at a time, drains ``run_once`` until the
queue is empty (``"none"``) or the per-run row cap is reached, and always
releases the lease (even on error). The job itself runs no SQL or HTTP — the
executor and repositories own the database (entrypoints-thin).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from supportagent.app.outbox import ExecResult
from supportagent.app.sync import SyncReport
from supportagent.entrypoints.worker.jobs import (
    ONBOARD_VERIFY_LEASE,
    OUTBOX_EXECUTE_LEASE,
    SYNC_POLL_LEASE_TTL_S,
    JobResult,
    onboard_verify,
    outbox_execute,
    sync_poll,
    sync_poll_lease,
)
from supportagent.ports.tickets import RecordType


class _FakeLease:
    """A ``Lease`` double that scripts the acquire outcome and records calls."""

    def __init__(self, *, acquire_result: bool = True) -> None:
        self._acquire_result = acquire_result
        self.acquired: list[tuple[str, str, int]] = []
        self.released: list[tuple[str, str]] = []

    def acquire(self, *, name: str, owner: str, ttl_s: int) -> bool:
        self.acquired.append((name, owner, ttl_s))
        return self._acquire_result

    def release(self, *, name: str, owner: str) -> None:
        self.released.append((name, owner))


class _FakeExecutor:
    """An ``OutboxExecutor`` double that scripts ``run_once`` results."""

    def __init__(
        self,
        results: list[ExecResult] | None = None,
        *,
        infinite: bool = False,
        raise_error: Exception | None = None,
    ) -> None:
        self._results = list(results) if results is not None else []
        self._infinite = infinite
        self._raise_error = raise_error
        self.calls: list[datetime] = []

    def run_once(self, now: datetime) -> ExecResult:
        self.calls.append(now)
        if self._raise_error is not None:
            raise self._raise_error
        if self._infinite:
            return ExecResult(status="sent")
        if self._results:
            return self._results.pop(0)
        return ExecResult(status="none")


def test_outbox_execute_skips_when_lease_held(clock: Any) -> None:
    """Another replica holds the lease → this run is a no-op (03 §2.5)."""
    lease = _FakeLease(acquire_result=False)
    executor = _FakeExecutor()
    res = outbox_execute(executor=executor, lease=lease, clock=clock, owner="owner-1")
    assert res == JobResult(name=OUTBOX_EXECUTE_LEASE, ran=False, rows=0)
    # Never held the lease → never release it, and never drive the executor.
    assert lease.acquired == [(OUTBOX_EXECUTE_LEASE, "owner-1", 10)]
    assert lease.released == []
    assert executor.calls == []


def test_outbox_execute_drains_until_none(clock: Any) -> None:
    """Holds the lease → drains the queue to empty, then releases (02 §7)."""
    lease = _FakeLease(acquire_result=True)
    executor = _FakeExecutor([ExecResult(status="sent"), ExecResult(status="reconciled")])
    res = outbox_execute(executor=executor, lease=lease, clock=clock, owner="owner-1")
    assert res.ran is True
    assert res.rows == 2
    # Two "processed" rows + the terminal "none" that ends the loop.
    assert len(executor.calls) == 3
    assert lease.released == [(OUTBOX_EXECUTE_LEASE, "owner-1")]


def test_outbox_execute_caps_at_max_rows(clock: Any) -> None:
    """A non-empty queue is capped at ``max_rows`` per run (02 §7: "or 50 rows")."""
    lease = _FakeLease(acquire_result=True)
    executor = _FakeExecutor(infinite=True)
    res = outbox_execute(executor=executor, lease=lease, clock=clock, owner="owner-1", max_rows=5)
    assert res.ran is True
    assert res.rows == 5
    assert len(executor.calls) == 5
    assert lease.released == [(OUTBOX_EXECUTE_LEASE, "owner-1")]


def test_outbox_execute_uses_default_row_cap(clock: Any) -> None:
    """Without an explicit cap, the job honours the 02 §7 default (50 rows)."""
    lease = _FakeLease(acquire_result=True)
    executor = _FakeExecutor(infinite=True)
    res = outbox_execute(executor=executor, lease=lease, clock=clock, owner="owner-1")
    assert res.rows == 50
    assert len(executor.calls) == 50


def test_outbox_execute_releases_lease_on_error(clock: Any) -> None:
    """A crash mid-drain still releases the lease (so a replica can retry)."""
    lease = _FakeLease(acquire_result=True)
    executor = _FakeExecutor(raise_error=RuntimeError("simulated crash"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        outbox_execute(executor=executor, lease=lease, clock=clock, owner="owner-1")
    assert lease.released == [(OUTBOX_EXECUTE_LEASE, "owner-1")]


class _FakePoller:
    """Delivers ``delivered`` records over ``pages`` pages, asking ``keep_going`` between them."""

    def __init__(
        self, *, delivered: int = 0, pages: int = 1, raise_error: Exception | None = None
    ) -> None:
        self._delivered = delivered
        self._pages = pages
        self._raise_error = raise_error
        self.tables: list[RecordType] = []
        self.pages_polled = 0

    def poll_table(
        self, table: RecordType, *, keep_going: Callable[[], bool] = lambda: True
    ) -> SyncReport:
        self.tables.append(table)
        if self._raise_error is not None:
            raise self._raise_error
        for page in range(1, self._pages + 1):
            self.pages_polled = page
            if page < self._pages and not keep_going():
                break
        return SyncReport(delivered=self._delivered, final_cursor=datetime(2026, 1, 1, tzinfo=UTC))


def test_sync_poll_lease_is_named_per_table() -> None:
    # 10-wp WP-008 / 02 §7: ``sync.poll.<table>``.
    assert sync_poll_lease(RecordType.INCIDENT) == "sync.poll.incident"


def test_sync_poll_runs_under_the_table_lease() -> None:
    lease = _FakeLease(acquire_result=True)
    poller = _FakePoller(delivered=3)
    res = sync_poll(poller=poller, table=RecordType.INCIDENT, lease=lease, owner="w1")
    assert res == JobResult(name="sync.poll.incident", ran=True, rows=3)
    assert poller.tables == [RecordType.INCIDENT]
    assert lease.acquired == [("sync.poll.incident", "w1", SYNC_POLL_LEASE_TTL_S)]
    assert lease.released == [("sync.poll.incident", "w1")]


def test_sync_poll_skips_when_lease_held() -> None:
    lease = _FakeLease(acquire_result=False)
    poller = _FakePoller()
    res = sync_poll(poller=poller, table=RecordType.INCIDENT, lease=lease, owner="w1")
    assert res == JobResult(name="sync.poll.incident", ran=False)
    assert poller.tables == []
    assert lease.released == [], "never release a lease someone else holds"


class _ScriptedLease(_FakeLease):
    """Acquire answers come from a script: the first take, then each renewal."""

    def __init__(self, answers: list[bool]) -> None:
        super().__init__()
        self._answers = answers

    def acquire(self, *, name: str, owner: str, ttl_s: int) -> bool:
        self.acquired.append((name, owner, ttl_s))
        return self._answers.pop(0)


def test_sync_poll_renews_the_lease_after_each_page() -> None:
    # BL-029: a long backlog (an LLM call per record) must not outlive the TTL.
    lease = _FakeLease(acquire_result=True)
    poller = _FakePoller(delivered=7, pages=3)
    res = sync_poll(poller=poller, table=RecordType.INCIDENT, lease=lease, owner="w1")
    assert res == JobResult(name="sync.poll.incident", ran=True, rows=7)
    assert poller.pages_polled == 3
    assert lease.acquired == [("sync.poll.incident", "w1", SYNC_POLL_LEASE_TTL_S)] * 3
    assert lease.released == [("sync.poll.incident", "w1")]


def test_sync_poll_stops_when_another_worker_took_the_lease() -> None:
    # The take succeeds, the first renewal finds the lease taken over: stop there.
    lease = _ScriptedLease([True, False])
    poller = _FakePoller(delivered=2, pages=3)
    res = sync_poll(poller=poller, table=RecordType.INCIDENT, lease=lease, owner="w1")
    assert res.ran and poller.pages_polled == 1
    assert len(lease.acquired) == 2


def test_sync_poll_releases_lease_on_error() -> None:
    lease = _FakeLease(acquire_result=True)
    poller = _FakePoller(raise_error=RuntimeError("servicenow down"))
    with pytest.raises(RuntimeError, match="servicenow down"):
        sync_poll(poller=poller, table=RecordType.INCIDENT, lease=lease, owner="w1")
    assert lease.released == [("sync.poll.incident", "w1")]


def test_onboard_verify_counts_and_warns_about_each_unbound_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-0019 §6: a credential that can write now is unbound; the scheduler can alert on it."""
    lease = _FakeLease()
    outcomes = [("req-a", True, ""), ("req-b", False, "it can insert: INSERT ANY TABLE")]
    with caplog.at_level(logging.WARNING, logger="supportagent.worker"):
        res = onboard_verify(verify=lambda: outcomes, lease=lease, owner="w1")
    assert res == JobResult(name=ONBOARD_VERIFY_LEASE, ran=True, rows=1)
    assert [r.getMessage() for r in caplog.records] == [
        "onboard.verify: unbound req-b: it can insert: INSERT ANY TABLE"
    ]
    assert lease.released == [(ONBOARD_VERIFY_LEASE, "w1")]


def test_onboard_verify_skips_when_another_worker_holds_it() -> None:
    called: list[bool] = []
    res = onboard_verify(
        verify=lambda: called.append(True) or [],  # type: ignore[func-returns-value]
        lease=_FakeLease(acquire_result=False),
        owner="w1",
    )
    assert res == JobResult(name=ONBOARD_VERIFY_LEASE, ran=False) and called == []
