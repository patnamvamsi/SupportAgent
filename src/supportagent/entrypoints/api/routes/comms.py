"""``/api/v1/comms-plans/{id}[/approve|/revoke]`` (02 §2, ARCH §6.3, WP-102).

Approving a plan is approving its ``comms_plan`` proposal: the body carries
the ``payload_hash`` the approver saw, which for a plan is its ``plan_hash``.
That's the same approval path as any proposal (RBAC scope, no
self-approval, 409 on a stale hash). Revoking stops every later send at once,
because the trigger checks the plan's status on each insert.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from supportagent.app.setup import SessionUser
from supportagent.domain.rbac import Permission
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1/comms-plans")
_viewer = require_permission(Permission.VIEW)
_approver = require_permission(Permission.DECIDE_PROPOSAL)


class PlanDecision(BaseModel):
    payload_hash: str = Field(min_length=1)
    reason: str | None = None


@router.get("/{plan_id}")
def get_plan(
    plan_id: str,
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.comms_plans().get(plan_id)


@router.post("/{plan_id}/approve")
def approve_plan(
    plan_id: str,
    body: PlanDecision,
    user: SessionUser = Depends(_approver),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    plan = ctx.comms_plans().get(plan_id)
    done = ctx.approvals().approve(
        str(plan["proposal_id"]),
        user.id,
        seen_hash=body.payload_hash,
        reason=body.reason,
        grants=user.grants,
    )
    return {"plan_id": plan_id, "approval_id": done.approval_id, "status": "approved"}


@router.post("/{plan_id}/revoke")
def revoke_plan(
    plan_id: str,
    user: SessionUser = Depends(_approver),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    ctx.comms_plans().revoke(plan_id, user.id, grants=user.grants)
    return {"plan_id": plan_id, "status": "revoked"}
