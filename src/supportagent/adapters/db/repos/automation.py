"""``automation_actors`` (03 §2, ARCH §8.2): candidates from ``doctor --discover-automation``.

Each call is its own committed unit of work: the doctor runs outside any
other transaction. Candidates are written with ``confirmed_by`` NULL; only
an admin confirms them (v0.1 UI). An existing row, confirmed or not, is
never touched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from supportagent.adapters.db.tables import automation_actors


class SqlAutomationActors:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add_candidates(self, tenant_id: str, actors: Sequence[tuple[str, str]]) -> int:
        if not actors:
            return 0
        with self._engine.begin() as conn:
            existing = set(
                conn.execute(
                    sa.select(automation_actors.c.sys_user).where(
                        automation_actors.c.tenant_id == tenant_id
                    )
                ).scalars()
            )
            new = [(user, kind) for user, kind in actors if user not in existing]
            for user, kind in new:
                conn.execute(
                    sa.insert(automation_actors).values(
                        tenant_id=tenant_id, sys_user=user, kind=kind
                    )
                )
        return len(new)

    def summary(self, tenant_id: str) -> tuple[int, int]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.count(automation_actors.c.confirmed_by),
                ).where(automation_actors.c.tenant_id == tenant_id)
            ).one()
        return int(row[0]), int(row[1])


__all__ = ["SqlAutomationActors"]
