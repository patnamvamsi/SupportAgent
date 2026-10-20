"""Unit-of-work semantics (10-wp): commit on demand, roll back otherwise.

A write UoW must never auto-commit: an exception, or simply exiting the
``with`` block without ``commit()``, rolls everything back — on both engines.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.clock import FrozenClock


def _insert_user(uow: SqlUnitOfWork, uid: str, ts: int) -> None:
    uow.connection.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
            "VALUES (:uid, 't_1', 'a@example.com', 'A', 1, :ts)"
        ),
        {"uid": uid, "ts": ts},
    )


def _user_count(db: Engine) -> int:
    with db.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one())


def test_uow_rolls_back_on_exception(db: Engine, clock: FrozenClock) -> None:
    """A raised exception rolls the whole UoW back (10-wp)."""
    with pytest.raises(RuntimeError, match="boom"), SqlUnitOfWork(db) as uow:
        _insert_user(uow, "u_1", int(clock.now().timestamp()))
        raise RuntimeError("boom")
    assert _user_count(db) == 0, "a failed UoW must not persist any writes"


def test_uow_rolls_back_without_commit(db: Engine, clock: FrozenClock) -> None:
    """Exiting without ``commit()`` rolls back; an explicit ``commit()`` persists (10-wp)."""
    with SqlUnitOfWork(db) as uow:
        _insert_user(uow, "u_1", int(clock.now().timestamp()))
    assert _user_count(db) == 0, "exiting without commit() must roll back"

    with SqlUnitOfWork(db) as uow:
        _insert_user(uow, "u_1", int(clock.now().timestamp()))
        uow.commit()
    assert _user_count(db) == 1, "an explicit commit() must persist"


def test_uow_nested_block_joins_outer_transaction(db: Engine, clock: FrozenClock) -> None:
    """A nested ``with`` on the same UoW joins the outer transaction (WP-011).

    The inner ``commit()`` defers to the outermost block, so a service called
    inside another service's UoW (GateService inside TriageService) is atomic
    with it: the outer block exiting without commit discards both writes.
    """
    ts = int(clock.now().timestamp())
    uow = SqlUnitOfWork(db)
    with uow:
        _insert_user(uow, "u_outer", ts)
        with uow:
            _insert_user(uow, "u_inner", ts)
            uow.commit()  # deferred: the outer block owns the transaction
    assert _user_count(db) == 0, "an inner commit must not persist without the outer commit"

    with uow:
        _insert_user(uow, "u_outer", ts)
        with uow:
            _insert_user(uow, "u_inner", ts)
            uow.commit()
        uow.commit()
    assert _user_count(db) == 2, "the outer commit persists both blocks' writes"


def test_uow_nested_failure_poisons_outer_commit(db: Engine, clock: FrozenClock) -> None:
    """An exception in a nested block that the outer block swallows still rolls back."""
    ts = int(clock.now().timestamp())
    uow = SqlUnitOfWork(db)
    with uow:
        _insert_user(uow, "u_outer", ts)
        with pytest.raises(RuntimeError, match="inner"), uow:
            _insert_user(uow, "u_inner", ts)
            raise RuntimeError("inner")
        with pytest.raises(RuntimeError, match="rollback-only"):
            uow.commit()
    assert _user_count(db) == 0
