"""SQLite PRAGMA contract (03 §1): the engine applies them on *every* connection.

The ``db`` fixture (04 §3) provides the session engine selected by ``--db``;
these PRAGMAs are SQLite-specific, so the test skips on Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine


def test_fk_pragma_on_every_connection(db: Engine) -> None:
    """03 §1: foreign_keys=ON, journal_mode=WAL, busy_timeout=5000 per connection.

    ``db.dispose()`` empties the pool so each iteration forces a fresh DBAPI
    connection — the PRAGMAs must be re-applied every time, not just on the
    first connection of the pool.
    """
    if db.dialect.name != "sqlite":
        pytest.skip("SQLite-specific PRAGMAs (03 §1)")
    for _ in range(3):
        db.dispose()  # force a fresh DBAPI connection
        with db.connect() as conn:
            fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
            wal = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
            busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    assert fk == 1, "PRAGMA foreign_keys must be ON (03 §1)"
    assert wal == "wal", "journal_mode must be WAL (03 §1)"
    assert busy == 5000, "busy_timeout must be 5000 ms (03 §1)"
