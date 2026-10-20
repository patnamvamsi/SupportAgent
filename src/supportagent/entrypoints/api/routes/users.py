"""``/api/v1/users`` — list users and assign roles (admin only; 03 §2.1, BL-021).

Not in the 02 §2 table: added so a second user (an approver) can be given a
role in v0.0. Every role change after the first admin is made by an admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from supportagent.app.setup import SessionUser
from supportagent.domain.roles import Role
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_role

router = APIRouter(prefix="/api/v1/users")
_admin = require_role(Role.ADMIN)


class AssignRole(BaseModel):
    role: Role


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
    ctx.setup().assign_role(actor_id=user.id, user_id=user_id, role=body.role)
    return {"user_id": user_id, "role": body.role.value}
