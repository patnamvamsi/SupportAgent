"""Unit of work (10-wp): one connection, one transaction, commit on demand.

A write UoW must never auto-commit: an exception, or simply exiting the
``with`` block without ``commit()``, rolls everything back (10-wp). SQLite
write UoWs take ``BEGIN IMMEDIATE`` so concurrent writers serialize at
acquisition time instead of deadlocking mid-transaction (03 §2.5 lease
semantics assume a single-writer database).
"""

from __future__ import annotations

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
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None
        self._committed = False

    def __enter__(self) -> SqlUnitOfWork:
        connection = self._engine.connect()
        if self._engine.dialect.name == "sqlite":
            # 10-wp: the write UoW takes the write lock up front.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        self._connection = connection
        self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
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
        """Persist the transaction (10-wp: commit is explicit, never implicit)."""
        self._require_connection().commit()
        self._committed = True

    @property
    def connection(self) -> Connection:
        """The active DBAPI-level connection for this unit of work."""
        return self._require_connection()

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
