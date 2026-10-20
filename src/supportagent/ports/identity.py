"""Identity provider port (ARCH §6.4, 02 §2): OIDC authorization-code login.

``begin_login`` produces the redirect plus the per-login secrets (state, nonce,
PKCE verifier) that the caller keeps in the session; ``complete_login``
exchanges the code and returns verified identity claims, or raises
``AuthenticationFailed``.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class IdentityProvider(Protocol):
    def begin_login(self) -> LoginRequest: ...

    def complete_login(self, *, code: str, pending: LoginRequest) -> IdentityClaims: ...
