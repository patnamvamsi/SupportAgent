"""ServiceNow auth (02 §1): Basic or OAuth client-credentials.

The adapter only ever needs an ``Authorization`` header; ``ServiceNowHTTP``
calls ``auth.headers()`` before each request. ``BasicAuth`` is stateless.
``OAuthClientCredentials`` fetches a token via ``POST /oauth_token.do`` and
caches it until ``expires_in - 60s`` (02 §1); the clock is injected (01 §3) so
the cache window is deterministic under test.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

import httpx

from supportagent.domain.errors import ServiceNowError
from supportagent.ports.clock import Clock

TOKEN_REFRESH_MARGIN_S = 60.0


@runtime_checkable
class ServiceNowAuth(Protocol):
    """Produces the ``Authorization`` header for each request (02 §1)."""

    def headers(self) -> Mapping[str, str]:
        """The auth headers to attach to the next request."""
        ...


class BasicAuth:
    """Static HTTP Basic auth (02 §1 ``basic`` kind)."""

    def __init__(self, *, user: str, password: str) -> None:
        self._token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")

    def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Basic {self._token}"}


class OAuthClientCredentials:
    """OAuth client-credentials with a cached access token (02 §1).

    The token is fetched lazily on first use and re-fetched once the clock
    passes ``expires_at - 60s``. The token POST is a plain ``httpx`` call (not
    routed through ``ServiceNowHTTP``'s retry loop, which assumes table-API
    semantics).
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        http: httpx.Client,
        token_url: str,
        client_id: str,
        client_secret: str,
        clock: Clock,
        refresh_margin_s: float = TOKEN_REFRESH_MARGIN_S,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._refresh_margin_s = refresh_margin_s
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def headers(self) -> Mapping[str, str]:
        if self._token is None or self._should_refresh(self._clock.now()):
            self._refresh()
        assert self._token is not None  # noqa: S101 — _refresh() always sets it (or raises)
        return {"Authorization": f"Bearer {self._token}"}

    def _should_refresh(self, now: datetime) -> bool:
        expires_at = self._expires_at
        return expires_at is None or now >= expires_at - timedelta(seconds=self._refresh_margin_s)

    def _refresh(self) -> None:
        try:
            resp = self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise ServiceNowError(f"token request failed: {exc}", retryable=True) from exc  # noqa: TRY003
        if resp.status_code >= 400:  # noqa: PLR2004
            raise ServiceNowError(  # noqa: TRY003
                f"token endpoint returned {resp.status_code}", retryable=True
            )
        payload = resp.json()
        try:
            token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceNowError("malformed token response", retryable=True) from exc  # noqa: TRY003
        self._token = token
        self._expires_at = self._clock.now() + timedelta(seconds=expires_in)


__all__ = ["TOKEN_REFRESH_MARGIN_S", "BasicAuth", "OAuthClientCredentials", "ServiceNowAuth"]
