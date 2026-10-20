"""``GET /api/v1/stats?mode=shadow&window=30d`` (02 §2): the mode-separated report."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends

from supportagent.app.setup import SessionUser
from supportagent.app.stats import parse_window
from supportagent.domain.rbac import Permission
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1/stats")
_viewer = require_permission(Permission.VIEW)


@router.get("")
def stats(
    mode: Literal["shadow", "assisted"] = "shadow",
    window: str = "30d",
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.stats().report(window=parse_window(window), mode=mode).as_dict()


@router.get("/promotion")
def promotion(
    window: str = "all",
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    """CAP §4.7 promotion gate plus approval fatigue (11-wp WP-115)."""
    stats = ctx.stats()
    span = parse_window(window)
    return {
        "promotion": stats.promotion(ctx.promotion_gate, window=span).as_dict(),
        "approval_fatigue": stats.fatigue(window=span).as_dict(),
    }
