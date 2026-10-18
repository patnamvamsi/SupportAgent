"""Worker jobs (02 §7, 10-wp WP-006).

Every job runs under a DB lease (03 §2.5) so that at most one process runs a
given job at a time. ``outbox.execute`` (02 §7, 2 s interval, v0.0) drains the
outbox: it drives ``OutboxExecutor.run_once`` until the queue is empty
(``"none"``) or the per-run row cap (50 rows) is reached.

This module is a thin entrypoint (entrypoints-thin): it acquires the lease,
drives the executor, and releases the lease. It never runs SQL or HTTP itself
— the executor and repositories own the database, the sink owns ServiceNow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from supportagent.app.outbox import ExecResult
    from supportagent.ports.clock import Clock
    from supportagent.ports.leases import Lease


class Executor(Protocol):
    """The narrow executor surface the job drives (02 §7).

    The job only needs ``run_once``; it depends on this abstraction rather
    than the concrete ``OutboxExecutor``, so a test double structurally
    satisfies it.
    """

    def run_once(self, now: datetime) -> ExecResult: ...


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


__all__ = [
    "OUTBOX_EXECUTE_LEASE",
    "OUTBOX_EXECUTE_MAX_ROWS",
    "Executor",
    "JobResult",
    "outbox_execute",
]
