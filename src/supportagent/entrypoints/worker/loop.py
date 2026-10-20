"""The worker loop (02 §4 ``supportagent worker``, 02 §7).

``Worker`` runs a fixed set of ``ScheduledJob``s, each at its own interval.
Every job takes its own DB lease (``jobs.py``), so replicas can run the same
loop safely: a run whose lease is held elsewhere is a no-op. One job failing
(ServiceNow down, a database blip) is logged and retried at its next
interval; it never stops the other jobs or the loop.

``run_once`` runs every job one time (``worker --once``, tests); ``run``
loops until ``stop()`` — called from a signal handler or the ``run``
command's shutdown — and finishes the job in progress before returning.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from supportagent.entrypoints.worker.jobs import JobResult

logger = logging.getLogger("supportagent.worker")


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One job and how often it runs (02 §7 interval column)."""

    name: str
    interval_s: float
    run: Callable[[], JobResult]


class Worker:
    """Runs scheduled jobs until stopped (02 §7)."""

    def __init__(
        self,
        jobs: Sequence[ScheduledJob],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not jobs:
            raise ValueError("a worker needs at least one job")  # noqa: TRY003
        self._jobs = list(jobs)
        self._monotonic = monotonic
        self._stop = threading.Event()

    @property
    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs)

    def stop(self) -> None:
        """Ask the loop to return after the job in progress (SIGTERM drain)."""
        self._stop.set()

    def run_once(self) -> list[JobResult]:
        """Run every job once; a failed job is logged and left out of the result."""
        results: list[JobResult] = []
        for job in self._jobs:
            result = self._run(job)
            if result is not None:
                results.append(result)
        return results

    def run(self) -> None:
        """Run each job at its interval until ``stop()``; the first round is immediate."""
        start = self._monotonic()
        due = {job.name: start for job in self._jobs}
        while not self._stop.is_set():
            for job in self._jobs:
                if self._stop.is_set():
                    break
                if self._monotonic() >= due[job.name]:
                    self._run(job)
                    due[job.name] = self._monotonic() + job.interval_s
            wait = min(due.values()) - self._monotonic()
            self._stop.wait(timeout=max(0.0, wait))

    def _run(self, job: ScheduledJob) -> JobResult | None:
        try:
            result = job.run()
        except Exception:
            logger.exception("job %s failed; retrying at its next interval", job.name)
            return None
        if result.ran:
            logger.info("job %s ran: %d row(s)", job.name, result.rows)
        return result


__all__ = ["ScheduledJob", "Worker"]
