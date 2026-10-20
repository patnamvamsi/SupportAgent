"""Double-submit CSRF (02 §2): cookie ``sa_csrf`` must equal header ``X-CSRF-Token``.

Every non-GET request needs it, including ``/auth/logout``. The cookie is set at
login and is readable by the page (not HttpOnly) so HTMX can echo it in the
header; an attacker's cross-site request can send the cookie but cannot read
it to forge the header.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.problems import problem

COOKIE = "sa_csrf"
HEADER = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def issue(response: Response, ctx: ApiContext) -> None:
    response.set_cookie(
        COOKIE,
        secrets.token_urlsafe(32),
        max_age=int(ctx.session_ttl.total_seconds()),
        httponly=False,
        secure=ctx.secure_cookies,
        samesite="lax",
        path="/",
    )


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _csrf(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _SAFE_METHODS:
            cookie = request.cookies.get(COOKIE)
            header = request.headers.get(HEADER)
            if not cookie or not header or not hmac.compare_digest(cookie, header):
                return problem(request, 403, "csrf_failed", f"missing or mismatched {HEADER}")
        return await call_next(request)
