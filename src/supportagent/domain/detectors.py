"""Secret detector classes (01 §7.13): the closed set of redaction classes.

A ``DetectorClass`` is one family of secret (email, AWS key, ...). The enum
*value* is the class's lowercase name — it is what appears in a ``redact``
event's ``counts_by_class`` and as the placeholder label (``<EMAIL_1>``).
``CUSTOM`` is the tenant-provided pattern slot (INV-06 excludes it from the
golden set; tenant patterns are wired in a later WP).
"""

from __future__ import annotations

from enum import StrEnum


class DetectorClass(StrEnum):
    """One redaction class (01 §7.13); value == lowercase class name."""

    EMAIL = "email"
    AWS_ACCESS_KEY = "aws_access_key"
    GENERIC_API_KEY = "generic_api_key"
    BEARER_TOKEN = "bearer_token"  # noqa: S105 — an enum member name, not a secret
    JWT = "jwt"
    PRIVATE_KEY_BLOCK = "private_key_block"
    CONNECTION_STRING = "connection_string"
    CARD_NUMBER = "card_number"
    PHONE_E164 = "phone_e164"
    CUSTOM = "custom"
