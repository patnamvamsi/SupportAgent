"""``SchemaProbe`` (02 §4.1 doctor ``db``): applied revision and SQLite foreign keys."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from supportagent.domain.errors import ExternalServiceError


class SqlSchemaProbe:
    """Reads ``alembic_version`` on a fresh connection; never writes."""

    def __init__(self, engine: Engine, *, head: str) -> None:
        self._engine = engine
        self._head = head

    def head_revision(self) -> str:
        return self._head

    def current_revision(self) -> str | None:
        try:
            with self._engine.connect() as conn:
                if not sa.inspect(conn).has_table("alembic_version"):
                    return None
                return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        except SQLAlchemyError as exc:
            raise ExternalServiceError(f"database unreachable: {exc}") from exc  # noqa: TRY003

    def foreign_keys_enforced(self) -> bool | None:
        if self._engine.dialect.name != "sqlite":
            return None
        with self._engine.connect() as conn:
            return bool(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())


__all__ = ["SqlSchemaProbe"]
