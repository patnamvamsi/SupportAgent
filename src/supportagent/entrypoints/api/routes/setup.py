"""``/api/v1/setup`` (02 §2): first-run state and the one-batch T1 grant approval."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from supportagent.app.setup import SessionUser
from supportagent.domain.roles import Role
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_role

router = APIRouter(prefix="/api/v1/setup")
_admin = require_role(Role.ADMIN)


@router.get("")
def setup_state(
    _user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    state = ctx.setup().state()
    return {
        "has_admin": state.has_admin,
        "default_grants": [
            {
                "kind": g.kind.value,
                "scope": g.scope,
                "status": g.status,
                "grant_id": g.grant_id,
            }
            for g in state.default_grants
        ],
    }


@router.post("/approve")
def approve_setup(
    user: SessionUser = Depends(_admin),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    approved = ctx.setup().run_setup(approver_id=user.id, requested_by=ctx.config_marker)
    return {"approved": approved}
