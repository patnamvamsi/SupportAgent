"""INV-01 driver-error translation (10-wp, 01 §6).

The INV-01 enforcement points (the ``trg_outbox_inv01`` /
``trg_no_self_approval`` / ``trg_outbox_immutable`` triggers, migration 0003)
raise a driver error whose message is prefixed ``INV-01:``. This module is the
single place that recognises that prefix and maps it to the typed domain error
``AuthorizationDenied`` (01 §6) with the correct ``code``:

* ``INV-01: outbox row lacks valid authorization`` → ``no_valid_authorization``
* ``INV-01: self-approval is not allowed``         → ``self_approval``
* ``INV-01: outbox authorization/payload is immutable`` → ``immutable``

Every other driver error is left untouched (10-wp: do not mask real failures).
"""

from __future__ import annotations

from typing import Literal

import sqlalchemy as sa

from supportagent.domain.errors import AuthorizationDenied, SupportAgentError

# The closed set of INV-01 ``AuthorizationDenied.code`` values (01 §6).
AuthCode = Literal["no_valid_authorization", "self_approval", "immutable", "rbac"]

# The single prefix every INV-01 enforcement point (trigger, CHECK) emits, so
# the DBAPI error can be recognised without matching a full sentence.
INV01_PREFIX = "INV-01:"

# Message fragment → ``AuthorizationDenied.code`` (01 §6). Order matters: the
# first matching fragment wins, so the most specific ("self-approval",
# "immutable") is checked before the generic "authorization".
_CODE_MARKERS: tuple[tuple[str, AuthCode], ...] = (
    ("self-approval", "self_approval"),
    ("immutable", "immutable"),
    ("authorization", "no_valid_authorization"),
)


def driver_message(exc: sa.exc.DBAPIError) -> str:
    """The human message of the underlying driver error (first arg), else the
    SQLAlchemy wrapping message."""
    orig = exc.orig
    if isinstance(orig, BaseException) and orig.args:
        first = orig.args[0]
        if isinstance(first, str):
            return first
    return str(exc)


def authorization_code(message: str) -> AuthCode:
    """The ``AuthorizationDenied.code`` for an INV-01 message (01 §6).

    Falls back to ``no_valid_authorization`` when no specific marker matches,
    which is the correct default for the generic "lacks valid authorization"
    trigger and for any future INV-01 message.
    """
    for marker, code in _CODE_MARKERS:
        if marker in message:
            return code
    return "no_valid_authorization"


def to_domain_error(exc: sa.exc.DBAPIError) -> SupportAgentError | None:
    """Map an INV-01 driver error to ``AuthorizationDenied`` (01 §6).

    Returns ``None`` when the error is *not* an INV-01 rejection, so the caller
    re-raises it unchanged. This keeps real failures (syntax errors, FK
    violations, network errors) visible instead of masking them as an
    authorization denial (10-wp).
    """
    message = driver_message(exc)
    if not message.startswith(INV01_PREFIX):
        return None
    return AuthorizationDenied(message, code=authorization_code(message))


__all__ = ["INV01_PREFIX", "AuthCode", "authorization_code", "driver_message", "to_domain_error"]
