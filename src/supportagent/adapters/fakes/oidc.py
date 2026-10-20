"""FakeOIDC (04 §1, 10-wp WP-012): an in-memory identity provider for tests and e2e.

``begin_login`` returns a redirect to a fake authorize URL carrying the state;
the test plays the user at the IdP with ``authorize(state, claims)``, which
returns a one-time code; ``complete_login`` redeems it, checking that the code
was issued for this login's state.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from supportagent.domain.errors import AuthenticationFailed
from supportagent.ports.identity import IdentityClaims, LoginRequest

AUTHORIZE_URL = "https://fake-oidc.test/authorize"


class FakeOIDC:
    def __init__(self) -> None:
        self._codes: dict[str, tuple[str, IdentityClaims]] = {}

    def discover(self) -> str:
        """The fake issuer is always reachable."""
        return "https://fake-idp.test"

    def begin_login(self) -> LoginRequest:
        state = secrets.token_urlsafe(16)
        nonce = secrets.token_urlsafe(16)
        query = urlencode({"state": state, "nonce": nonce})
        return LoginRequest(
            url=f"{AUTHORIZE_URL}?{query}",
            state=state,
            nonce=nonce,
            code_verifier=secrets.token_urlsafe(32),
        )

    def authorize(self, state: str, claims: IdentityClaims) -> str:
        """The user signs in at the IdP: issue a one-time code for ``state``."""
        code = secrets.token_urlsafe(16)
        self._codes[code] = (state, claims)
        return code

    def complete_login(self, *, code: str, pending: LoginRequest) -> IdentityClaims:
        entry = self._codes.pop(code, None)
        if entry is None or entry[0] != pending.state:
            raise AuthenticationFailed("unknown or replayed authorization code")  # noqa: TRY003
        return entry[1]
