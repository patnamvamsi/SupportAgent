"""The worker loop (02 §4 ``supportagent worker``, 02 §7)."""

from __future__ import annotations

import pytest

from supportagent.entrypoints.worker.jobs import JobResult
from supportagent.entrypoints.worker.loop import ScheduledJob, Worker


class _Clock:
    """A monotonic clock the loop advances by waiting (the test drives time)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _job(name: str, interval: float, log: list[tuple[str, float]], clock: _Clock) -> ScheduledJob:
    def run() -> JobResult:
        log.append((name, clock.t))
        return JobResult(name=name, ran=True, rows=1)

    return ScheduledJob(name=name, interval_s=interval, run=run)


def test_run_once_runs_every_job_and_isolates_failures() -> None:
    clock = _Clock()
    log: list[tuple[str, float]] = []

    def boom() -> JobResult:
        raise RuntimeError("servicenow down")  # noqa: TRY003

    worker = Worker(
        [_job("a", 60, log, clock), ScheduledJob("bad", 60, boom), _job("b", 60, log, clock)],
        monotonic=clock,
    )
    results = worker.run_once()
    assert [r.name for r in results] == ["a", "b"], "a failed job never stops the others"
    assert [name for name, _ in log] == ["a", "b"]


def test_run_schedules_each_job_at_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    log: list[tuple[str, float]] = []
    worker = Worker([_job("fast", 2, log, clock), _job("slow", 5, log, clock)], monotonic=clock)
    waits: list[float] = []

    def fake_wait(timeout: float | None = None) -> bool:
        assert timeout is not None
        waits.append(timeout)
        clock.t += timeout
        if clock.t >= 10:
            worker.stop()
        return worker._stop.is_set()

    monkeypatch.setattr(worker._stop, "wait", fake_wait)
    worker.run()
    assert [t for n, t in log if n == "fast"] == [0, 2, 4, 6, 8]
    assert [t for n, t in log if n == "slow"] == [0, 5], "stop() wins over a job falling due"
    assert all(w >= 0 for w in waits)


def test_stop_before_run_returns_immediately() -> None:
    clock = _Clock()
    log: list[tuple[str, float]] = []
    worker = Worker([_job("a", 60, log, clock)], monotonic=clock)
    worker.stop()
    worker.run()
    assert log == []


def test_worker_needs_a_job() -> None:
    with pytest.raises(ValueError, match="at least one job"):
        Worker([])
