"""Lease semantics (03 §2.5): exclusive acquire, expiry, takeover, renewal.

``leases`` is the single global lock table (no tenant_id, 03 §2.5). Acquire is
an ``INSERT ... ON CONFLICT(name) DO UPDATE ... WHERE expired-or-mine`` —
rowcount 1 means the lease was acquired.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from supportagent.adapters.db.repos.leases import LeaseRepo, SqlLease
from supportagent.adapters.db.tables import leases
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.clock import FrozenClock


def _lease(db: Engine, name: str) -> tuple[str, int]:
    with db.connect() as conn:
        row = conn.execute(
            select(leases.c.owner, leases.c.until_ts).where(leases.c.name == name)
        ).one()
    return row[0], row[1]


def test_lease_acquire_exclusive(db: Engine, uow: SqlUnitOfWork, clock: FrozenClock) -> None:
    """A second owner cannot take a live lease; the current owner can renew (03 §2.5)."""
    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="a", ttl_s=60) is True
        uow.commit()

    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="b", ttl_s=60) is False
        uow.commit()

    # Winner still owns the lease, for the full ttl from acquire time.
    owner, until = _lease(db, "worker")
    assert owner == "a"
    assert until == int(clock.now().timestamp()) + 60

    # The current owner re-acquiring is a renewal, not a conflict (03 §2.5 WHERE clause).
    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="a", ttl_s=60) is True
        uow.commit()


def test_lease_expires_and_is_taken_over(
    db: Engine, uow: SqlUnitOfWork, clock: FrozenClock
) -> None:
    """Once ``until_ts`` passes, another owner may take the lease over (03 §2.5)."""
    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="a", ttl_s=60) is True
        uow.commit()

    clock.advance(61)  # past expiry

    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="b", ttl_s=60) is True
        uow.commit()

    owner, until = _lease(db, "worker")
    assert owner == "b"
    assert until == int(clock.now().timestamp()) + 60


def test_lease_release(db: Engine, uow: SqlUnitOfWork, clock: FrozenClock) -> None:
    """A released lease is immediately acquirable by another owner."""
    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="a", ttl_s=60) is True
        uow.commit()

    with uow:
        LeaseRepo(uow, clock).release(name="worker", owner="a")
        uow.commit()

    with uow:
        assert LeaseRepo(uow, clock).acquire(name="worker", owner="b", ttl_s=60) is True
        uow.commit()

    owner, _ = _lease(db, "worker")
    assert owner == "b"


def test_sql_lease_commits_each_call(db: Engine, clock: FrozenClock) -> None:
    """``SqlLease`` (the worker's Lease port) is visible to other replicas at once."""
    a = SqlLease(SqlUnitOfWork(db), clock)
    b = SqlLease(SqlUnitOfWork(db), clock)
    assert a.acquire(name="sync.poll.incident", owner="a", ttl_s=60) is True
    assert _lease(db, "sync.poll.incident")[0] == "a"  # committed, seen on a new connection
    assert b.acquire(name="sync.poll.incident", owner="b", ttl_s=60) is False
    b.release(name="sync.poll.incident", owner="b")  # not the holder: a no-op
    assert _lease(db, "sync.poll.incident")[0] == "a"
    a.release(name="sync.poll.incident", owner="a")
    assert b.acquire(name="sync.poll.incident", owner="b", ttl_s=60) is True
