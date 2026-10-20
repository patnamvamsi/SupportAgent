"""``/api/v1/cases/{id}`` and ``/api/v1/traces/{trace_id}`` (02 §2)."""

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


@router.get("/cases/{case_id}")
def get_case(
    case_id: str,
    _user: SessionUser = Depends(_viewer),  # noqa: B008
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> dict[str, object]:
    return ctx.queries().case(case_id).as_dict()


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
