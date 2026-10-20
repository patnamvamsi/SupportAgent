"""``GET /api/v1/stats?mode=shadow&window=30d`` (02 §2): the mode-separated report."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends

from supportagent.app.setup import SessionUser
from supportagent.app.stats import parse_window
from supportagent.domain.roles import Role
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_role

router = APIRouter(prefix="/api/v1/stats")
_viewer = require_role(Role.VIEWER)


@router.get("")
def stats(
    mode: Literal["shadow", "assisted"] = "shadow",
    window: str = "30d",
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.stats().report(window=parse_window(window), mode=mode).as_dict()
