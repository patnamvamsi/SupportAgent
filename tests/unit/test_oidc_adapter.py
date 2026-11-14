"""WP-012: the Authlib/joserfc OIDC adapter — discovery, PKCE, nonce, id-token checks.

The IdP is an ``httpx.MockTransport`` serving discovery, the token endpoint and
JWKS, with id tokens signed by a throwaway RSA key.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from supportagent.adapters.identity.oidc import OIDCClient
from supportagent.domain.errors import AuthenticationFailed
from supportagent.ports.identity import IdentityClaims

ISSUER = "https://idp.test/tenant"
CLIENT_ID = "client-123"
REDIRECT = "https://sa.test/auth/callback"


class _IdP:
    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.claims_override: dict[str, Any] = {}
        self.issuer_in_discovery = ISSUER
        self.token_requests: list[dict[str, list[str]]] = []
        self.jwks_hits = 0
        self.nonce = ""
        self.end_session: str | None = f"{ISSUER}/logout"

    def _id_token(self) -> str:
        now = int(self.clock.now().timestamp())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "sub-ada",
            "email": "ada@acme.test",
            "name": "Ada",
            "nonce": self.nonce,
            "iat": now,
            "exp": now + 300,
        }
        claims.update(self.claims_override)
        return jwt.encode({"alg": "RS256", "kid": self.key.kid}, claims, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer_in_discovery,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    **({"end_session_endpoint": self.end_session} if self.end_session else {}),
                },
            )
        if path.endswith("/jwks"):
            self.jwks_hits += 1
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path.endswith("/token"):
            self.token_requests.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"id_token": self._id_token(), "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def idp(clock: Any) -> _IdP:
    return _IdP(clock)


@pytest.fixture
def client(idp: _IdP, clock: Any) -> OIDCClient:
    return OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",  # noqa: S106 — fake IdP
        redirect_url=REDIRECT,
        http=httpx.Client(transport=httpx.MockTransport(idp.handler)),
        clock=clock,
    )


def test_begin_login_uses_pkce_s256_state_and_nonce(client: OIDCClient) -> None:
    start = client.begin_login()
    query = parse_qs(urlparse(start.url).query)
    assert start.url.startswith(f"{ISSUER}/authorize?")
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT]
    assert "openid" in query["scope"][0].split()
    assert query["state"] == [start.state]
    assert query["nonce"] == [start.nonce]
    assert query["code_challenge_method"] == ["S256"]
    digest = hashlib.sha256(start.code_verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert len({start.state, start.nonce, start.code_verifier}) == 3


def test_complete_login_verifies_id_token(client: OIDCClient, idp: _IdP) -> None:
    start = client.begin_login()
    idp.nonce = start.nonce
    claims = client.complete_login(code="the-code", pending=start)
    assert claims == IdentityClaims(sub="sub-ada", email="ada@acme.test", name="Ada")
    sent = idp.token_requests[0]
    assert sent["code"] == ["the-code"]
    assert sent["code_verifier"] == [start.code_verifier]
    assert sent["redirect_uri"] == [REDIRECT]


_TAMPERS: dict[str, Callable[[_IdP], None]] = {
    "wrong-nonce": lambda idp: idp.claims_override.update(nonce="other"),
    "wrong-audience": lambda idp: idp.claims_override.update(aud="someone-else"),
    "wrong-issuer": lambda idp: idp.claims_override.update(iss="https://evil.test"),
    "expired": lambda idp: idp.claims_override.update(exp=1),
    "no-sub": lambda idp: idp.claims_override.update(sub=None),
    "foreign-key": lambda idp: setattr(
        idp, "key", RSAKey.generate_key(2048, parameters={"kid": "k1"})
    ),
}


@pytest.mark.parametrize("tamper", list(_TAMPERS), ids=list(_TAMPERS))
def test_complete_login_rejects_bad_id_token(client: OIDCClient, idp: _IdP, tamper: str) -> None:
    start = client.begin_login()  # discovery + JWKS cached with the original key
    client._jwks()
    idp.nonce = start.nonce
    _TAMPERS[tamper](idp)
    with pytest.raises(AuthenticationFailed):
        client.complete_login(code="the-code", pending=start)


def test_key_rotation_refreshes_jwks_once(client: OIDCClient, idp: _IdP) -> None:
    # A token signed with a new kid triggers exactly one JWKS refresh.
    start = client.begin_login()
    client._jwks()
    assert idp.jwks_hits == 1
    idp.key = RSAKey.generate_key(2048, parameters={"kid": "k2"})
    idp.nonce = start.nonce
    claims = client.complete_login(code="the-code", pending=start)
    assert claims.sub == "sub-ada"
    assert idp.jwks_hits == 2


def test_discovery_issuer_mismatch_rejected(client: OIDCClient, idp: _IdP) -> None:
    idp.issuer_in_discovery = "https://evil.test"
    with pytest.raises(AuthenticationFailed, match="issuer"):
        client.begin_login()


def test_token_endpoint_error_is_authentication_failure(clock: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        return httpx.Response(400, content=json.dumps({"error": "invalid_grant"}))

    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",  # noqa: S106 — fake IdP
        redirect_url=REDIRECT,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
    )
    start = client.begin_login()
    with pytest.raises(AuthenticationFailed, match="token endpoint"):
        client.complete_login(code="bad", pending=start)


def test_discover_returns_the_verified_issuer(client: OIDCClient) -> None:
    # doctor oidc (02 §4.1).
    assert client.discover() == ISSUER


def test_discover_rejects_a_mismatched_issuer(client: OIDCClient, idp: _IdP) -> None:
    idp.issuer_in_discovery = "https://evil.test"
    with pytest.raises(AuthenticationFailed, match="issuer"):
        client.discover()


def test_backchannel_carries_server_requests_but_not_the_browser_url(idp: _IdP, clock: Any) -> None:
    """Local install: the browser sees the public issuer, the app reaches the IdP in Docker."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(f"{request.url.scheme}://{request.url.host}:{request.url.port}")
        return idp.handler(request)

    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",  # noqa: S106 — fake IdP
        redirect_url=REDIRECT,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        backchannel_url="http://keycloak:8080",
    )
    start = client.begin_login()
    assert start.url.startswith(f"{ISSUER}/authorize?")  # the browser's URL is public
    idp.nonce = start.nonce
    assert client.complete_login(code="c", pending=start).sub == "sub-ada"  # iss still ISSUER
    assert set(hosts) == {"http://keycloak:8080"}  # discovery, token and JWKS


def test_complete_login_keeps_the_id_token_for_sign_out(client: OIDCClient, idp: _IdP) -> None:
    start = client.begin_login()
    idp.nonce = start.nonce
    claims = client.complete_login(code="the-code", pending=start)
    assert claims.id_token.count(".") == 2
    assert claims.id_token not in repr(claims), "never logged"


def test_end_session_url_returns_to_the_login_page(client: OIDCClient) -> None:
    url = client.end_session_url(id_token_hint="the-token")  # noqa: S106 — fake token
    assert url is not None and url.startswith(f"{ISSUER}/logout?")
    query = parse_qs(urlparse(url).query)
    assert query["id_token_hint"] == ["the-token"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["post_logout_redirect_uri"] == ["https://sa.test/ui/login"]
    assert "id_token_hint" not in parse_qs(
        urlparse(client.end_session_url(id_token_hint="") or "").query
    )


def test_end_session_url_is_none_without_an_endpoint(idp: _IdP, clock: Any) -> None:
    idp.end_session = None
    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",  # noqa: S106 — fake IdP
        redirect_url=REDIRECT,
        http=httpx.Client(transport=httpx.MockTransport(idp.handler)),
        clock=clock,
    )
    assert client.end_session_url(id_token_hint="t") is None  # noqa: S106 — fake token


def test_end_session_url_is_none_when_the_idp_is_down(clock: Any) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="shh",  # noqa: S106 — fake IdP
        redirect_url=REDIRECT,
        http=httpx.Client(transport=httpx.MockTransport(down)),
        clock=clock,
    )
    assert client.end_session_url(id_token_hint="t") is None  # noqa: S106 — fake token
