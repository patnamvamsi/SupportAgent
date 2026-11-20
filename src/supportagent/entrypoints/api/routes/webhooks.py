"""``POST /api/v1/webhooks/{source}`` (02 §2, ARCH §7; WP-113, WP-213).

Authenticated per source, never by a session: a ``generic`` source by HMAC
(``X-SA-Signature: sha256=<hex>`` over ``X-SA-Timestamp + "." + body``, plus a
single-use ``X-SA-Nonce``); an ``alertmanager`` or ``grafana`` source by
``Authorization: Bearer <secret>``. The source must be configured. A valid
delivery is queued and answered **202** with ``accepted``: the inbox id for a
generic delivery, the list of ids (one per alert) for an alert batch.
A bad signature or token, a stale timestamp, a replayed nonce or an unknown source is
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
        service = ctx.webhooks()
        ids = service.receive(source, dict(request.headers), body)
    except WebhookRejected as exc:
        if exc.reason == "bad_payload":
            return problem(request, 422, "invalid", "the body does not map to an alert")
        return problem(request, 401, "unauthenticated", "webhook rejected")
    accepted: str | list[str] = ids if service.is_batch(source) else ids[0]
    return JSONResponse({"accepted": accepted}, status_code=202)
