"""Dialect-specific ``INSERT ... ON CONFLICT DO UPDATE`` (03 §2.5, §3).

Both supported dialects have upserts (SQLite ≥ 3.24, Postgres 9.5+), but
SQLAlchemy only exposes the statement builder through the dialect-specific
constructors — this helper picks the right one so the repos stay
dialect-agnostic. The two engines are the only ones this project runs on
(04 §6: ``--db sqlite|postgres``), so anything else is a configuration
error, not a fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.expression import ClauseElement
from sqlalchemy.sql.schema import Column, Table

from supportagent.domain.errors import ConfigError


# PLR0913/PLR0917: thin dialect dispatcher — all six parameters are part of
# the explicit upsert contract (conflict target, SET, DO-UPDATE guard).
def upsert_stmt(  # noqa: PLR0913, PLR0917
    engine: Engine,
    table: Table,
    values: dict[str, object],
    index_elements: Sequence[Column[Any] | str],
    set_: dict[str, object],
    where: ClauseElement | None = None,
) -> Insert:
    """Build ``INSERT ... ON CONFLICT (<index_elements>) DO UPDATE ...``.

    ``where`` (optional) is the DO UPDATE guard, e.g. the lease takeover
    predicate (03 §2.5). A no-op update reports rowcount 0 on both engines,
    which the repos use as the "did not win" signal.

    The dialect ``Insert`` builders are structurally compatible but mypy
    sees them as distinct types, so each branch returns through the common
    core ``Insert`` base; the optional ``where`` guard is a SQLAlchemy marker
    role (``WhereHavingRole``) that mypy cannot verify structurally, hence
    the cast — the runtime isinstance check is done by SQLAlchemy.
    """
    name = engine.dialect.name
    if name == "postgresql":
        return cast(
            Insert,
            pg_insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=index_elements,
                set_=set_,
                where=cast(Any, where),
            ),
        )
    if name == "sqlite":
        return cast(
            Insert,
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_update(
                index_elements=index_elements,
                set_=set_,
                where=cast(Any, where),
            ),
        )
    raise ConfigError(  # noqa: TRY003
        f"dialect '{name}' is unsupported (need sqlite or postgresql, 04 §6)"
    )
