"""Request dependencies (02 §2): the API context, ``current_user``, ``require_permission``."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request

from supportagent.app.setup import SessionUser
from supportagent.domain.errors import AuthenticationFailed, AuthorizationDenied
from supportagent.domain.rbac import Permission
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


def require_permission(permission: Permission) -> Callable[[SessionUser], SessionUser]:
    """Users holding ``permission`` in some scope (11-wp WP-103).

    The route's action then checks the permission against its own target
    (an approver's service scope is checked by ``ApprovalService``).
    """

    def _check(user: SessionUser = Depends(current_user)) -> SessionUser:  # noqa: B008
        if not user.can(permission, anywhere=True):
            raise AuthorizationDenied(  # noqa: TRY003
                f"requires the {permission.value!r} permission", code="rbac"
            )
        return user

    return _check
