"""RFC 9457 problem+json responses (02 §2): ``{type, title, status, detail, trace_id}``."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from supportagent.domain.errors import (
    AuthenticationFailed,
    AuthorizationDenied,
    ConcurrencyConflict,
    DomainValidationError,
    NotFound,
)

MEDIA_TYPE = "application/problem+json"


def problem(request: Request, status: int, title: str, detail: str) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", None)
    return JSONResponse(
        {
            "type": f"/problems/{title}",
            "title": title,
            "status": status,
            "detail": detail,
            "trace_id": trace_id,
        },
        status_code=status,
        media_type=MEDIA_TYPE,
    )


def install(app: FastAPI) -> None:
    """Map the typed domain errors (01 §6) to their HTTP statuses."""

    @app.exception_handler(AuthenticationFailed)
    async def _unauthenticated(request: Request, exc: AuthenticationFailed) -> JSONResponse:
        return problem(request, 401, "unauthenticated", str(exc))

    @app.exception_handler(AuthorizationDenied)
    async def _forbidden(request: Request, exc: AuthorizationDenied) -> JSONResponse:
        # 02 §2: a hash that differs from the current one is a 409, not a 403.
        status = 409 if exc.code == "stale_proposal" else 403
        return problem(request, status, exc.code, str(exc))

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> JSONResponse:
        return problem(request, 404, "not_found", str(exc))

    @app.exception_handler(ConcurrencyConflict)
    async def _conflict(request: Request, exc: ConcurrencyConflict) -> JSONResponse:
        return problem(request, 409, "conflict", str(exc))

    @app.exception_handler(DomainValidationError)
    async def _invalid(request: Request, exc: DomainValidationError) -> JSONResponse:
        return problem(request, 422, "invalid", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(request, 422, "invalid_request", str(exc.errors()))
