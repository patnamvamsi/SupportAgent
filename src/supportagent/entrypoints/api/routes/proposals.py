"""``/api/v1/proposals`` (02 §2): list, detail, approve, reject."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from supportagent.app.setup import SessionUser
from supportagent.domain.rbac import Permission
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1/proposals")
_viewer = require_permission(Permission.VIEW)
_approver = require_permission(Permission.DECIDE_PROPOSAL)


class Decision(BaseModel):
    """The hash the approver saw: the decision is bound to exactly those bytes."""

    payload_hash: str = Field(min_length=1)
    reason: str | None = None


@router.get("")
def list_proposals(
    status: str | None = None,
    case_id: str | None = None,
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    items = ctx.queries().proposals(status=status, case_id=case_id)
    return {"items": [p.as_dict() for p in items]}


@router.get("/{proposal_id}")
def get_proposal(
    proposal_id: str,
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.queries().proposal(proposal_id).as_dict()


@router.post("/{proposal_id}/approve")
def approve(
    proposal_id: str,
    body: Decision,
    user: SessionUser = Depends(_approver),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str | None]:
    """409 ``stale_proposal`` on a hash mismatch; 403 ``self_approval`` (02 §2).

    A comms plan returns ``outbox_id: null``: approving it sends nothing.
    """
    done = ctx.approvals().approve(
        proposal_id, user.id, seen_hash=body.payload_hash, reason=body.reason, grants=user.grants
    )
    return {"approval_id": done.approval_id, "outbox_id": done.outbox_id}


@router.post("/{proposal_id}/reject")
def reject(
    proposal_id: str,
    body: Decision,
    user: SessionUser = Depends(_approver),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    """The reason is required (422 without one)."""
    approval_id = ctx.approvals().reject(
        proposal_id,
        user.id,
        seen_hash=body.payload_hash,
        reason=body.reason or "",
        grants=user.grants,
    )
    return {"approval_id": approval_id}
