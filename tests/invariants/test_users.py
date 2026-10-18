"""User rows (03 §2.1): save() upserts on the stable OIDC sub."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from supportagent.adapters.db.repos.users import UserRepo
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.model.user import User


def _user(**overrides: object) -> User:
    base: dict[str, object] = {
        "id": "sub-1",
        "tenant_id": "t_1",
        "name": "Alice",
        "email": "alice@acme.test",
        "is_active": True,
    }
    base.update(overrides)
    return User.model_validate(base)


def test_user_save_refreshes_profile_keeps_created_ts(db: Engine, uow: SqlUnitOfWork) -> None:
    repo = UserRepo(uow)
    with uow:
        repo.save(_user(), created_ts=100)
        uow.commit()
    with uow:
        repo.save(_user(name="Alice B", email="ab@acme.test", is_active=False), created_ts=200)
        uow.commit()

    with uow:
        assert repo.get("sub-1") == _user(name="Alice B", email="ab@acme.test", is_active=False)
    with db.connect() as conn:
        created = conn.execute(text("SELECT created_ts FROM users WHERE id = 'sub-1'")).scalar_one()
    assert created == 100, "created_ts is set once"
