"""Identity provider port (ARCH §6.4, 02 §2): OIDC authorization-code login.

``begin_login`` produces the redirect plus the per-login secrets (state, nonce,
PKCE verifier) that the caller keeps in the session; ``complete_login``
exchanges the code and returns verified identity claims, or raises
``AuthenticationFailed``. ``end_session_url`` is where sign-out sends the
browser so the IdP's session ends too (OIDC RP-Initiated Logout, BL-046).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """One pending login: the IdP redirect and the secrets to check on return."""

    url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class IdentityClaims:
    """Verified id-token claims; ``sub`` becomes ``users.id`` (ARCH §6.4)."""

    sub: str
    email: str
    name: str
    #: The raw id token, kept only as the ``id_token_hint`` for sign-out (BL-046).
    id_token: str = field(default="", compare=False, repr=False)


class IdentityProvider(Protocol):
    def begin_login(self) -> LoginRequest: ...

    def complete_login(self, *, code: str, pending: LoginRequest) -> IdentityClaims: ...

    def end_session_url(self, *, id_token_hint: str) -> str | None:
        """The IdP's sign-out URL, returning to ``/ui/login``; ``None`` if it has none."""
        ...
