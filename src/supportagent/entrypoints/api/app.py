"""The FastAPI app (02 §2). ``create_app(ctx)`` takes its wiring from bootstrap."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from ulid import ULID

from supportagent import __version__
from supportagent.domain.errors import ExternalServiceError
from supportagent.entrypoints.api import csrf, problems
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.routes import (
    auth,
    cases,
    comms,
    grants,
    proposals,
    setup,
    stats,
    ui,
    users,
    webhooks,
)

__all__ = ["ApiContext", "create_app"]

#: 02 §3: no inline script or style; forms post only to this origin; no framing.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(title="SupportAgent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    problems.install(app)
    # Middleware added later runs first: the trace id must exist before the
    # CSRF check can put it in a problem body.
    csrf.install(app)

    @app.middleware("http")
    async def _trace(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.trace_id = f"tr_{ULID()}"
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict[str, str]:
        """02 §2: the config loaded (or there'd be no app), the database is reachable
        and its migrations are at this build's head. 503 otherwise; the app never
        migrates itself (06 §2)."""
        body = {"version": __version__}
        schema = ctx.schema
        if schema is None:
            return {"status": "ready", **body}
        try:
            current = schema.current_revision()
        except ExternalServiceError:
            response.status_code = 503
            return {"status": "not_ready", "reason": "database unreachable", **body}
        head = schema.head_revision()
        if current != head:
            response.status_code = 503
            reason = f"database at {current or 'no schema'}, this build at {head}: run migrate"
            return {"status": "not_ready", "reason": reason, **body}
        return {"status": "ready", "revision": head, **body}

    for module in (auth, setup, grants, proposals, cases, comms, users, stats, webhooks, ui):
        app.include_router(module.router)
    return app
