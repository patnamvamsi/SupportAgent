"""User rows (03 §2.1): upsert + read-back over the ``users`` table."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from supportagent.adapters.db.tables import users
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.model.user import User


class UserRepo:
    """Typed access to the ``users`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def save(self, user: User, *, created_ts: int) -> None:
        """Insert or refresh a user row (03 §2.1: stable ``id``, ``created_ts`` once set)."""
        values = {
            "id": user.id,
            "tenant_id": user.tenant_id,
            "email": user.email,
            "name": user.name,
            "is_active": int(user.is_active),
            "created_ts": created_ts,
        }
        # on_conflict_do_nothing is dialect-specific (SQLite/Postgres); both
        # are supported here (04 §6).
        insert = (pg_insert if self._uow.engine.dialect.name == "postgresql" else sqlite_insert)(
            users
        )
        self._uow.connection.execute(
            insert.values(**values).on_conflict_do_nothing(index_elements=["id"])
        )

    def get(self, user_id: str) -> User | None:
        """Load one user by id, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(users).where(users.c.id == user_id))
            .mappings()
            .first()
        )
        if row is None:
            return None
        return User(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            email=row["email"],
            is_active=bool(row["is_active"]),
        )
