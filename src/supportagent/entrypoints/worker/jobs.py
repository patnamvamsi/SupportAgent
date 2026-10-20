"""Worker jobs (02 §7, 10-wp WP-006 / WP-008).

Every job runs under a DB lease (03 §2.5) so that at most one process runs a
given job at a time. ``outbox.execute`` (02 §7, 2 s interval, v0.0) drains the
outbox: it drives ``OutboxExecutor.run_once`` until the queue is empty
(``"none"``) or the per-run row cap (50 rows) is reached.
``sync.poll.<table>`` (02 §7, ``poll_interval_s``) drives one
``SyncService.poll_table`` under the lease named for its table (10-wp WP-008).

This module is a thin entrypoint (entrypoints-thin): it acquires the lease,
drives the executor, and releases the lease. It never runs SQL or HTTP itself
— the executor and repositories own the database, the sink owns ServiceNow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from supportagent.app.outbox import ExecResult
    from supportagent.app.sync import SyncReport
    from supportagent.domain.deprecation import DeprecationNotice
    from supportagent.ports.clock import Clock
    from supportagent.ports.leases import Lease
    from supportagent.ports.tickets import SnTable


class Executor(Protocol):
    """The narrow executor surface the job drives (02 §7).

    The job only needs ``run_once``; it depends on this abstraction rather
    than the concrete ``OutboxExecutor``, so a test double structurally
    satisfies it.
    """

    def run_once(self, now: datetime) -> ExecResult: ...


class Poller(Protocol):
    """The narrow ``SyncService`` surface the poll job drives (02 §7)."""

    def poll_table(self, table: SnTable) -> SyncReport: ...


#: 02 §7: the job's lease name (one global lock per job, 03 §2.5).
OUTBOX_EXECUTE_LEASE = "outbox.execute"

#: 02 §7: the per-run row cap ("until empty or 50 rows").
OUTBOX_EXECUTE_MAX_ROWS = 50


@dataclass(frozen=True, slots=True)
class JobResult:
    """Outcome of one worker-job run (02 §7).

    ``ran`` is ``True`` iff this process held the lease and drained the queue;
    ``False`` means another replica held the lease and this run was a no-op.
    ``rows`` is the number of outbox rows this run processed (0 when not run).
    """

    name: str
    ran: bool
    rows: int = 0


def outbox_execute(  # noqa: PLR0913
    *,
    executor: Executor,
    lease: Lease,
    clock: Clock,
    owner: str,
    lease_ttl_s: int = 10,
    max_rows: int = OUTBOX_EXECUTE_MAX_ROWS,
) -> JobResult:
    """Drain the outbox under the ``outbox.execute`` lease (02 §7).

    Acquires the lease; if another replica holds it, returns ``ran=False``
    (this run is a no-op). Otherwise drives ``run_once`` until the queue is
    empty (``"none"``) or ``max_rows`` rows are processed, then releases the
    lease (even on error). Each ``run_once`` is one claimed row, so the loop
    is exactly "drain the queue" (02 §7).
    """
    if not lease.acquire(name=OUTBOX_EXECUTE_LEASE, owner=owner, ttl_s=lease_ttl_s):
        return JobResult(name=OUTBOX_EXECUTE_LEASE, ran=False)
    rows = 0
    try:
        for _ in range(max_rows):
            res = executor.run_once(clock.now())
            if res.status == "none":
                break
            rows += 1
    finally:
        lease.release(name=OUTBOX_EXECUTE_LEASE, owner=owner)
    return JobResult(name=OUTBOX_EXECUTE_LEASE, ran=True, rows=rows)


#: The poll lease is not renewed mid-run, so its TTL bounds one poll: a page of
#: new records can mean one LLM call each. A crashed holder stalls the table
#: for at most this long; a run that outlives it may overlap the next holder,
#: which ``sync_seen`` and INV-02 dedup absorb.
SYNC_POLL_LEASE_TTL_S = 900


def sync_poll_lease(table: SnTable) -> str:
    """02 §7 / 10-wp WP-008: one lease per polled table, ``sync.poll.<table>``."""
    return f"sync.poll.{table.value}"


def sync_poll(
    *,
    poller: Poller,
    table: SnTable,
    lease: Lease,
    owner: str,
    lease_ttl_s: int = SYNC_POLL_LEASE_TTL_S,
) -> JobResult:
    """One ``poll_table`` under the ``sync.poll.<table>`` lease (02 §7).

    ``ran=False`` when another replica holds the lease. ``rows`` is the count
    of records delivered to the inbound handler. The lease is released even
    when the poll raises; the cursor only advances per committed page, so the
    next run resumes where this one stopped (ARCH §8.1).
    """
    name = sync_poll_lease(table)
    if not lease.acquire(name=name, owner=owner, ttl_s=lease_ttl_s):
        return JobResult(name=name, ran=False)
    try:
        report = poller.poll_table(table)
    finally:
        lease.release(name=name, owner=owner)
    return JobResult(name=name, ran=True, rows=report.delivered)


#: 02 §7: ``llm.deprecation_check`` runs daily (11-wp WP-106).
LLM_DEPRECATION_LEASE = "llm.deprecation_check"
LLM_DEPRECATION_INTERVAL_S = 86_400

_log = logging.getLogger("supportagent.worker")


def llm_deprecation_check(
    *,
    notices: Callable[[], Sequence[DeprecationNotice]],
    lease: Lease,
    owner: str,
    lease_ttl_s: int = 300,
) -> JobResult:
    """Warn for every slot whose pinned model retires within 90 days (ARCH §9).

    A retired slot is logged as an error. Its calls already safe-default
    (``RetiredModelGuard``), and the operator has to re-pin and re-calibrate.
    """
    if not lease.acquire(name=LLM_DEPRECATION_LEASE, owner=owner, ttl_s=lease_ttl_s):
        return JobResult(name=LLM_DEPRECATION_LEASE, ran=False)
    try:
        found = list(notices())
        for n in found:
            if n.status == "retired":
                _log.error(
                    "llm slot %s: model %s retired on %s; calls safe-default until re-pinned",
                    n.slot,
                    n.model_id,
                    n.retires_on,
                )
            else:
                _log.warning(
                    "llm slot %s: model %s retires on %s (%d days): start the deprecation runbook",
                    n.slot,
                    n.model_id,
                    n.retires_on,
                    n.days_left,
                )
    finally:
        lease.release(name=LLM_DEPRECATION_LEASE, owner=owner)
    return JobResult(name=LLM_DEPRECATION_LEASE, ran=True, rows=len(found))


__all__ = [
    "LLM_DEPRECATION_INTERVAL_S",
    "LLM_DEPRECATION_LEASE",
    "OUTBOX_EXECUTE_LEASE",
    "OUTBOX_EXECUTE_MAX_ROWS",
    "SYNC_POLL_LEASE_TTL_S",
    "Executor",
    "JobResult",
    "Poller",
    "llm_deprecation_check",
    "outbox_execute",
    "sync_poll",
    "sync_poll_lease",
]
