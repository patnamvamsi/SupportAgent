"""Request dependencies (02 §2): the API context, ``current_user``, ``require_role``."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from supportagent.app.setup import SessionUser
from supportagent.domain.errors import AuthenticationFailed, AuthorizationDenied
from supportagent.domain.roles import Role
from supportagent.entrypoints.api import session
from supportagent.entrypoints.api.context import ApiContext


def context(request: Request) -> ApiContext:
    ctx: ApiContext = request.app.state.ctx
    return ctx


def current_user(request: Request, ctx: ApiContext = Depends(context)) -> SessionUser:  # noqa: B008
    """The signed-in, still-active user; roles are read fresh from the DB."""
    user_id = session.read(request, ctx).get("uid")
    if not isinstance(user_id, str):
        raise AuthenticationFailed("sign in required")  # noqa: TRY003
    user = ctx.setup().user(user_id)
    if user is None:
        raise AuthenticationFailed("the session's user is no longer active")  # noqa: TRY003
    return user


def require_role(role: Role) -> Callable[[SessionUser], SessionUser]:
    """A dependency allowing users whose roles rank at or above ``role``."""

    def _check(user: SessionUser = Depends(current_user)) -> SessionUser:  # noqa: B008
        if not user.has_role(role):
            raise AuthorizationDenied(f"requires role {role.value!r}", code="rbac")  # noqa: TRY003
        return user

    return _check
