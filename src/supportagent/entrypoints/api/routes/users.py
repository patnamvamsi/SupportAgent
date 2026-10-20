"""``/api/v1/users``: list users, assign and revoke roles (admin only; 03 §2.1, WP-103).

Every role change after the first admin is made by an admin and recorded as
a ``gate`` event. A role carries a scope, ``*`` or ``service:<name>``, and
the tenant's last admin can't be removed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from supportagent.app.setup import SessionUser
from supportagent.domain.rbac import ALL_SCOPE, Permission
from supportagent.domain.roles import Role
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1/users")
_admin = require_permission(Permission.ADMINISTER)


class AssignRole(BaseModel):
    role: Role
    scope: str = ALL_SCOPE


@router.get("")
def list_users(
    _user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return {"items": ctx.queries().users()}


@router.post("/{user_id}/roles")
def assign_role(
    user_id: str,
    body: AssignRole,
    user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    ctx.setup().assign_role(actor_id=user.id, user_id=user_id, role=body.role, scope=body.scope)
    return {"user_id": user_id, "role": body.role.value, "scope": body.scope}


@router.delete("/{user_id}/roles/{role}")
def revoke_role(
    user_id: str,
    role: Role,
    user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    """422 when it would remove the tenant's last admin; 404 when the role isn't held."""
    ctx.setup().revoke_role(actor_id=user.id, user_id=user_id, role=role)
    return {"user_id": user_id, "revoked": role.value}
