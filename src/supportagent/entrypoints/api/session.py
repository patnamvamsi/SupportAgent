"""Signed session cookie (02 §2): ``base64url(json).base64url(hmac-sha256)``.

Stdlib HMAC rather than a new dependency. The cookie carries only ids and the
per-login OIDC secrets, never roles: roles are re-read from the database on
every request, so a revoked role takes effect immediately. Every payload has an
``exp`` (epoch seconds) checked on read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from fastapi import Request, Response

from supportagent.entrypoints.api.context import ApiContext

COOKIE = "sa_session"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def encode(data: dict[str, Any], secret: str) -> str:
    payload = _b64(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
    return f"{payload}.{_sign(payload, secret)}"


def decode(value: str, secret: str, *, now_ts: int) -> dict[str, Any]:
    """The session data, or ``{}`` when the cookie is absent, forged or expired."""
    payload, _, sig = value.partition(".")
    if not sig or not hmac.compare_digest(sig, _sign(payload, secret)):
        return {}
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("exp"), int):
        return {}
    return data if data["exp"] > now_ts else {}


def read(request: Request, ctx: ApiContext) -> dict[str, Any]:
    raw = request.cookies.get(COOKIE)
    if raw is None:
        return {}
    return decode(raw, ctx.session_secret, now_ts=int(ctx.clock.now().timestamp()))


def write(response: Response, ctx: ApiContext, data: dict[str, Any], *, ttl_s: int) -> None:
    exp = int(ctx.clock.now().timestamp()) + ttl_s
    response.set_cookie(
        COOKIE,
        encode({**data, "exp": exp}, ctx.session_secret),
        max_age=ttl_s,
        httponly=True,
        secure=ctx.secure_cookies,
        samesite="lax",
        path="/",
    )


def clear(response: Response, ctx: ApiContext) -> None:
    response.delete_cookie(COOKIE, path="/", secure=ctx.secure_cookies, httponly=True)
