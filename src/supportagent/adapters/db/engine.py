"""SQLAlchemy engine factory (03 §1).

SQLite connections need per-connection PRAGMAs — ``foreign_keys=ON``,
``journal_mode=WAL``, ``busy_timeout=5000`` (03 §1). PRAGMAs are not
inherited by pooled connections, so they are applied in the ``connect``
event on *every* new DBAPI connection, not once at engine creation.
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def make_engine(url: str) -> Engine:
    """Create a SQLAlchemy engine for ``url`` with the 03 §1 dialect contract."""
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _apply_pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine
