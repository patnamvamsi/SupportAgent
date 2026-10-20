"""``POST /api/v1/webhooks/{source}`` (02 §2, ARCH §7; 11-wp WP-113).

Authenticated by HMAC, not by a session: ``X-SA-Signature: sha256=<hex>``
over ``X-SA-Timestamp + "." + body``, plus a single-use ``X-SA-Nonce``. The
source must be configured. A valid delivery is queued and answered **202**.
A bad signature, a stale timestamp, a replayed nonce or an unknown source is
**401**, and a body that doesn't map to an alert is **422**. Every rejection
is a ``webhook_rejected`` event. The response never says which check failed
beyond that split, so a probe learns nothing about the configured sources.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from supportagent.domain.webhook import WebhookRejected
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context
from supportagent.entrypoints.api.problems import problem

router = APIRouter(prefix="/api/v1/webhooks")


@router.post("/{source}", status_code=202)
async def receive(
    source: str,
    request: Request,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> JSONResponse:
    body = await request.body()
    try:
        item_id = ctx.webhooks().receive(source, dict(request.headers), body)
    except WebhookRejected as exc:
        if exc.reason == "bad_payload":
            return problem(request, 422, "invalid", "the body does not map to an alert")
        return problem(request, 401, "unauthenticated", "webhook rejected")
    return JSONResponse({"accepted": item_id}, status_code=202)
