"""Unit of work (10-wp): one connection, one transaction, commit on demand.

A write UoW must never auto-commit: an exception, or simply exiting the
``with`` block without ``commit()``, rolls everything back (10-wp). SQLite
write UoWs take ``BEGIN IMMEDIATE`` so concurrent writers serialize at
acquisition time instead of deadlocking mid-transaction (03 §2.5 lease
semantics assume a single-writer database).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType

from sqlalchemy.engine import Connection, Engine


class SqlUnitOfWork:
    """A unit of work bound to one database connection.

    Usage::

        with SqlUnitOfWork(engine) as uow:
            repo.save(...)
            uow.commit()  # persist; omit to roll back

    The instance is reusable across successive ``with`` blocks; each block
    gets a fresh connection and transaction.

    Nested ``with`` blocks on the same instance join the outer transaction
    (10-wp WP-011: proposals are submitted in the triage UoW). Only the
    outermost block owns the transaction: an inner ``commit()`` is deferred to
    it, and an exception leaving an inner block marks the transaction
    rollback-only, so a later outer ``commit()`` raises instead of persisting
    a half-done unit.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None
        self._committed = False
        self._depth = 0
        self._rollback_only = False

    def __enter__(self) -> SqlUnitOfWork:
        if self._depth > 0:  # nested: join the outer transaction
            self._depth += 1
            return self
        connection = self._engine.connect()
        if self._engine.dialect.name == "sqlite":
            # 10-wp: the write UoW takes the write lock up front.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        self._connection = connection
        self._committed = False
        self._rollback_only = False
        self._depth = 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._depth > 1:  # nested: the outermost block decides
            self._depth -= 1
            if exc_type is not None:
                self._rollback_only = True
            return
        self._depth = 0
        connection = self._connection
        committed = self._committed
        self._connection = None
        self._committed = False
        if connection is not None:
            # 10-wp: anything that did not reach commit() is rolled back.
            if exc_type is not None or not committed:
                connection.rollback()
            connection.close()

    def commit(self) -> None:
        """Persist the transaction (10-wp: commit is explicit, never implicit).

        Inside a nested block this is deferred to the outermost block.
        """
        connection = self._require_connection()
        if self._rollback_only:
            raise RuntimeError(  # noqa: TRY003
                "SqlUnitOfWork is rollback-only: a nested block raised"
            )
        if self._depth > 1:
            return
        connection.commit()
        self._committed = True

    @property
    def connection(self) -> Connection:
        """The active DBAPI-level connection for this unit of work."""
        return self._require_connection()

    @contextmanager
    def reading(self) -> Iterator[Connection]:
        """A read that takes no write lock: inside an active block, its transaction;
        otherwise a connection of its own, so a read made while another unit of work
        writes (a live catalog reloading during triage) doesn't wait on SQLite's lock."""
        if self._connection is not None:
            yield self._connection
            return
        with self._engine.connect() as connection:
            yield connection

    @property
    def engine(self) -> Engine:
        """The engine this unit of work was built from (for dialect dispatch)."""
        return self._engine

    def _require_connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError(  # noqa: TRY003
                "SqlUnitOfWork is not active — use it inside a 'with' block"
            )
        return self._connection
