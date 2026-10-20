"""Double-submit CSRF (02 §2): cookie ``sa_csrf`` must equal header ``X-CSRF-Token``.

Every non-GET request needs it, including ``/auth/logout``. The cookie is set at
login and is readable by the page (not HttpOnly) so a script can echo it in the
header; an attacker's cross-site request can send the cookie but cannot read
it to forge the header.

Admin UI forms (``/ui/*``, urlencoded, no JavaScript) carry the token in a
hidden ``csrf_token`` field instead. The middleware cannot read a form body
without consuming it, so it lets such posts through and every UI POST handler
parses its form with ``verify_form`` — one helper, tested over every route.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable, Mapping
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response

from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.problems import problem

COOKIE = "sa_csrf"
HEADER = "X-CSRF-Token"
FORM_FIELD = "csrf_token"
_FORM_TYPE = "application/x-www-form-urlencoded"


class CsrfError(Exception):
    """A UI form's ``csrf_token`` did not match the ``sa_csrf`` cookie."""


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
            header = request.headers.get(HEADER)
            if header is None and _is_ui_form(request):
                return await call_next(request)  # checked by verify_form in the handler
            if not _matches(request.cookies.get(COOKIE), header):
                return problem(request, 403, "csrf_failed", f"missing or mismatched {HEADER}")
        return await call_next(request)

    @app.exception_handler(CsrfError)
    async def _form_failed(request: Request, exc: CsrfError) -> Response:
        return problem(request, 403, "csrf_failed", str(exc))


async def verify_form(request: Request) -> Mapping[str, str]:
    """Parse a UI form body and check its ``csrf_token`` against the cookie."""
    fields = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if not _matches(request.cookies.get(COOKIE), fields.get(FORM_FIELD)):
        raise CsrfError(f"missing or mismatched {FORM_FIELD}")  # noqa: TRY003
    return fields


def _is_ui_form(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return request.url.path.startswith("/ui/") and content_type.startswith(_FORM_TYPE)


def _matches(cookie: str | None, presented: str | None) -> bool:
    return bool(cookie) and bool(presented) and hmac.compare_digest(str(cookie), str(presented))
