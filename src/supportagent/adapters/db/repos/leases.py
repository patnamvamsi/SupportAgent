"""Lease semantics (03 §2.5): one global lock table, atomic acquire.

Acquire is a single ``INSERT ... ON CONFLICT(name) DO UPDATE ... WHERE
expired-or-mine`` statement: a new row, a takeover of an expired lease, or a
renewal by the current owner all yield rowcount 1; a live lease held by
someone else is a no-op with rowcount 0 — on both engines.
"""

from __future__ import annotations

import sqlalchemy as sa

from supportagent.adapters.db.tables import leases
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.db.upsert import upsert_stmt
from supportagent.ports.clock import Clock


class LeaseRepo:
    """Typed access to the ``leases`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    def acquire(self, *, name: str, owner: str, ttl_s: int) -> bool:
        """Try to take the lease for ``name`` (03 §2.5).

        Returns ``True`` iff this call holds the lease afterwards: fresh
        acquisition, renewal by the current owner, or takeover of an expired
        lease. A live lease held by another owner returns ``False``.
        """
        now = int(self._clock.now().timestamp())
        until = now + ttl_s
        # SQLAlchemy memoizes rowcount for UPDATE/DELETE only; on psycopg3 a
        # closed cursor forgets it, so an INSERT would report -1. Ask the
        # engine to preserve it (03 §2.5: rowcount 1 == acquired).
        result = self._uow.connection.execute(
            upsert_stmt(
                self._uow.engine,
                leases,
                {"name": name, "owner": owner, "until_ts": until},
                [leases.c.name],
                {"owner": owner, "until_ts": until},
                where=sa.or_(leases.c.until_ts < now, leases.c.owner == owner),
            ),
            execution_options={"preserve_rowcount": True},
        )
        return int(result.rowcount) == 1

    def release(self, *, name: str, owner: str) -> None:
        """Release the lease if (and only if) we hold it (03 §2.5)."""
        self._uow.connection.execute(
            sa.delete(leases).where(
                leases.c.name == name,
                leases.c.owner == owner,
            )
        )
