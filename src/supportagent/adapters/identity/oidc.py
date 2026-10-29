"""OIDC authorization-code login with PKCE (ARCH §6.4, 10-wp WP-012).

Authlib supplies the PKCE and token helpers; joserfc (Authlib's JOSE library)
verifies the id token. Checks, all fail-closed as ``AuthenticationFailed``:

- discovery: ``issuer`` must equal the configured issuer exactly;
- token exchange: ``authorization_code`` + ``code_verifier`` (S256), client
  credentials via HTTP Basic;
- id token: signature against the IdP's JWKS (fetched once, re-fetched once
  when a ``kid`` is unknown — key rotation), ``iss``, ``aud``, ``exp`` (60 s
  leeway), ``nonce`` equal to this login's nonce, and a non-empty ``sub``.

An unreachable IdP is an ``ExternalServiceError`` (retryable), not a failed
login.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from supportagent.domain.errors import AuthenticationFailed, ExternalServiceError
from supportagent.ports.clock import Clock
from supportagent.ports.identity import IdentityClaims, LoginRequest

_ALGORITHMS = ("RS256", "PS256", "ES256")
_LEEWAY_S = 60
_SCOPE = "openid email profile"


class OIDCClient:
    """The real identity provider adapter (``IdentityProvider`` port)."""

    def __init__(  # noqa: PLR0913 — the OIDC client registration
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        http: httpx.Client,
        clock: Clock,
        backchannel_url: str | None = None,
    ) -> None:
        self._issuer = issuer
        self._public = _origin(issuer)
        self._backchannel = _origin(backchannel_url) if backchannel_url else None
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_url = redirect_url
        self._http = http
        self._clock = clock
        self._metadata: dict[str, Any] | None = None
        self._keys: KeySet | None = None

    def begin_login(self) -> LoginRequest:
        meta = self._discovery()
        state = generate_token(32)
        nonce = generate_token(32)
        verifier = generate_token(64)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_url,
                "scope": _SCOPE,
                "state": state,
                "nonce": nonce,
                "code_challenge": create_s256_code_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return LoginRequest(
            url=f"{meta['authorization_endpoint']}?{query}",
            state=state,
            nonce=nonce,
            code_verifier=verifier,
        )

    def complete_login(self, *, code: str, pending: LoginRequest) -> IdentityClaims:
        meta = self._discovery()
        response = self._request(
            "POST",
            str(meta["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_url,
                "code_verifier": pending.code_verifier,
            },
            auth=(self._client_id, self._client_secret),
        )
        if response.status_code != httpx.codes.OK:
            raise AuthenticationFailed(  # noqa: TRY003
                f"token endpoint refused the code (HTTP {response.status_code})"
            )
        id_token = response.json().get("id_token")
        if not isinstance(id_token, str):
            raise AuthenticationFailed("token endpoint returned no id_token")  # noqa: TRY003
        claims = self._verify(id_token, pending.nonce)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthenticationFailed("id token has no subject")  # noqa: TRY003
        email = claims.get("email") if isinstance(claims.get("email"), str) else ""
        name = next(
            (
                v
                for v in (claims.get("name"), claims.get("preferred_username"), email, sub)
                if isinstance(v, str) and v
            ),
            sub,
        )
        return IdentityClaims(sub=sub, email=str(email), name=str(name))

    # --- helpers ------------------------------------------------------------

    def _verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            try:
                token = jwt.decode(id_token, self._jwks(), algorithms=list(_ALGORITHMS))
            except InvalidKeyIdError:
                # Unknown kid (rotation): refresh the key set once and retry.
                token = jwt.decode(id_token, self._jwks(refresh=True), algorithms=list(_ALGORITHMS))
            registry = jwt.JWTClaimsRegistry(
                now=lambda: int(self._clock.now().timestamp()),
                leeway=_LEEWAY_S,
                iss={"essential": True, "value": self._issuer},
                aud={"essential": True, "value": self._client_id},
                exp={"essential": True},
                sub={"essential": True},
                nonce={"essential": True, "value": nonce},
            )
            registry.validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise AuthenticationFailed(f"id token rejected: {exc}") from exc  # noqa: TRY003
        return dict(token.claims)

    def discover(self) -> str:
        """Fetch (or reuse) the discovery document; the verified issuer (``doctor`` oidc)."""
        return str(self._discovery()["issuer"])

    def _discovery(self) -> dict[str, Any]:
        if self._metadata is None:
            url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
            response = self._request("GET", url)
            if response.status_code != httpx.codes.OK:
                raise ExternalServiceError(  # noqa: TRY003
                    f"OIDC discovery failed (HTTP {response.status_code})", retryable=True
                )
            meta = response.json()
            if meta.get("issuer") != self._issuer:
                raise AuthenticationFailed(  # noqa: TRY003
                    f"discovery issuer {meta.get('issuer')!r} does not match {self._issuer!r}"
                )
            self._metadata = meta
        return self._metadata

    def _jwks(self, *, refresh: bool = False) -> KeySet:
        if self._keys is None or refresh:
            response = self._request("GET", str(self._discovery()["jwks_uri"]))
            if response.status_code != httpx.codes.OK:
                raise ExternalServiceError(  # noqa: TRY003
                    f"OIDC JWKS fetch failed (HTTP {response.status_code})", retryable=True
                )
            self._keys = KeySet.import_key_set(response.json())
        return self._keys

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        if self._backchannel is not None and url.startswith(self._public + "/"):
            # Server-to-IdP traffic goes over the backchannel; the browser never sees it.
            url = self._backchannel + url[len(self._public) :]
        try:
            return self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"OIDC provider unreachable: {exc}", retryable=True) from exc  # noqa: TRY003


def _origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``."""
    parsed = httpx.URL(url)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.host}{port}"
