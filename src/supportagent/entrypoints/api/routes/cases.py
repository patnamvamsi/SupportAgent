"""``/api/v1/cases/{id}``, its hand-back, and ``/api/v1/traces/{trace_id}`` (02 §2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from supportagent.app.replay import summarize
from supportagent.app.setup import SessionUser
from supportagent.domain.errors import NotFound
from supportagent.domain.rbac import Permission
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context, require_permission

router = APIRouter(prefix="/api/v1")
_viewer = require_permission(Permission.VIEW)
_analyst = require_permission(Permission.HAND_BACK)


@router.get("/cases/{case_id}")
def get_case(
    case_id: str,
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.queries().case(case_id).as_dict()


@router.post("/cases/{case_id}/hand-back")
def hand_back(
    case_id: str,
    user: SessionUser = Depends(_analyst),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, str]:
    """``human_owned → in_progress`` (02 §2, ARCH §8.2); scoped to the case's service."""
    ctx.lifecycle().hand_back(case_id, user.id, grants=user.grants)
    return {"case_id": case_id, "status": "in_progress"}


@router.get("/traces/{trace_id}")
def get_trace(
    trace_id: str,
    user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    """The replay view (same data as ``supportagent replay``), tenant-checked."""
    view = ctx.replay(trace_id)
    if view.tenant_id != user.tenant_id:  # trace ids are global (BL-015): never leak
        raise NotFound(f"trace not found: {trace_id!r}")  # noqa: TRY003
    return {
        "trace_id": view.trace_id,
        "events": [
            {
                "ts": e.ts.isoformat(),
                "kind": e.kind.value,
                "actor": e.actor,
                "status": e.status,
                "model": e.model,
                "tokens": {"in": e.tokens_in, "out": e.tokens_out},
                "latency_ms": e.latency_ms,
                "span_id": e.span_id,
                "parent_span_id": e.parent_span_id,
                "summary": summarize(e.kind, e.payload),
            }
            for e in view.events
        ],
    }
