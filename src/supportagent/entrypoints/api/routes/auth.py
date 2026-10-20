"""``/auth/*`` (02 §2): OIDC login with state, nonce and PKCE; session; logout."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse

from supportagent.domain.errors import AuthenticationFailed
from supportagent.domain.roles import Role
from supportagent.entrypoints.api import csrf, session
from supportagent.entrypoints.api.context import ApiContext
from supportagent.entrypoints.api.deps import context
from supportagent.ports.identity import LoginRequest

router = APIRouter()


@router.get("/auth/login")
def login(ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    pending = ctx.identity.begin_login()
    response = RedirectResponse(pending.url, status_code=302)
    session.write(
        response,
        ctx,
        {
            "oidc": {
                "state": pending.state,
                "nonce": pending.nonce,
                "code_verifier": pending.code_verifier,
            }
        },
        ttl_s=int(ctx.login_ttl.total_seconds()),
    )
    return response


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str,
    state: str,
    ctx: ApiContext = Depends(context),  # noqa: B008
) -> Response:
    pending = session.read(request, ctx).get("oidc")
    if not isinstance(pending, dict) or not hmac.compare_digest(
        str(pending.get("state", "")), state
    ):
        raise AuthenticationFailed("login state mismatch or expired; start again")  # noqa: TRY003
    claims = ctx.identity.complete_login(
        code=code,
        pending=LoginRequest(
            url="",
            state=str(pending["state"]),
            nonce=str(pending["nonce"]),
            code_verifier=str(pending["code_verifier"]),
        ),
    )
    user = ctx.setup().login(claims)
    target = "/ui/setup" if user.has_role(Role.ADMIN) else "/ui/approvals"
    response = RedirectResponse(target, status_code=303)
    # A fresh cookie value replaces the pre-login one (no session fixation).
    session.write(response, ctx, {"uid": user.id}, ttl_s=int(ctx.session_ttl.total_seconds()))
    csrf.issue(response, ctx)
    return response


@router.post("/auth/logout", status_code=204)
def logout(ctx: ApiContext = Depends(context)) -> Response:  # noqa: B008
    response = Response(status_code=204)
    session.clear(response, ctx)
    response.delete_cookie(csrf.COOKIE, path="/")
    return response
