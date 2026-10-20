"""The FastAPI app (02 §2). ``create_app(ctx)`` takes its wiring from bootstrap."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from ulid import ULID

from supportagent.entrypoints.api import csrf, problems
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.routes import (
    auth,
    cases,
    grants,
    proposals,
    setup,
    stats,
    ui,
    users,
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

    for module in (auth, setup, grants, proposals, cases, users, stats, ui):
        app.include_router(module.router)
    return app
