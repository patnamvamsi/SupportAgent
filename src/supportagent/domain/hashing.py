"""Canonical JSON and hashing (01 §7.1): stable, compact, float-free."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping

from supportagent.domain.errors import DomainValidationError

# A JSON value (01 §7.1): floats are NOT part of the domain of hashed values.
type JsonValue = bool | int | str | list[JsonValue] | dict[str, JsonValue] | None

_FLOAT_ERROR = "floats are not allowed in hashed payloads; use int or str"


def _reject_floats(value: object) -> None:
    """Reject anything outside the hashable JSON domain (01 §7.1).

    Floats get the pinned message; every other non-JSON value (set, bytes,
    datetime, non-str key, ...) is also a ``DomainValidationError`` here,
    instead of a ``TypeError`` from ``json.dumps`` at hashing time.
    """
    if isinstance(value, float):
        raise DomainValidationError(_FLOAT_ERROR)
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DomainValidationError(  # noqa: TRY003 — names the offending key
                    f"hashed payload keys must be str, got {type(key).__name__}"
                )
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)
    else:
        raise DomainValidationError(  # noqa: TRY003 — names the offending type
            f"{type(value).__name__} is not a JSON value; hashed payloads take "
            "None, bool, int, str, list and str-keyed dict only"
        )


def _normalize(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    """Canonical form (01 §7.1): sorted keys, compact separators, unicode kept,
    NFC-normalized strings, no floats anywhere."""
    _reject_floats(value)
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: object) -> str:
    """sha256 hex of the canonical form (64 lowercase hex chars, 01 §7.1)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plan_hash(
    *,
    audience: str,
    channel: str,
    template_id: str,
    allowed_fields: Mapping[str, str],
    cadence_minutes: int | None,
) -> str:
    """Hash of a comms plan's identity (01 §7.1)."""
    return payload_hash(
        {
            "audience": audience,
            "channel": channel,
            "template_id": template_id,
            "allowed_fields": dict(allowed_fields),
            "cadence_minutes": cadence_minutes,
        }
    )
