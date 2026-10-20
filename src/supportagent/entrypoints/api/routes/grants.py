"""``/api/v1/grants`` (02 §2): list, approve, revoke — admin only."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from supportagent.app.setup import SessionUser
from supportagent.domain.rbac import Permission
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1/grants")
_admin = require_permission(Permission.ADMINISTER)


class ApproveGrant(BaseModel):
    valid_days: int = Field(ge=1, le=365)


@router.get("")
def list_grants(
    status: str | None = None,
    _user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return {"items": ctx.queries().grants(status=status)}


@router.post("/{grant_id}/approve")
def approve_grant(
    grant_id: str,
    body: ApproveGrant,
    user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    """403 when the caller requested the grant (the CHECK, mapped by ``SetupService``)."""
    ctx.setup().approve_grant(grant_id, approver_id=user.id, valid_days=body.valid_days)
    return {"grant_id": grant_id, "status": "approved"}


@router.post("/{grant_id}/revoke")
def revoke_grant(
    grant_id: str,
    user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    ctx.setup().revoke_grant(grant_id, actor_id=user.id)
    return {"grant_id": grant_id, "status": "revoked"}
