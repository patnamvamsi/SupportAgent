"""WP-001: canonical JSON and hashing (01 §7.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.hashing import canonical_json, payload_hash, plan_hash


def test_canonical_json_sorted_and_compact() -> None:
    value = {"b": 1, "a": ["x", {"d": None, "c": True}], "z": "é"}
    expected = '{"a":["x",{"c":true,"d":null}],"b":1,"z":"é"}'
    assert canonical_json(value) == expected
    # Matches the canonical serialization exactly: sorted keys, compact
    # separators, unicode not escaped (01 §7.1).
    assert canonical_json(value) == json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert ": " not in canonical_json(value)
    assert ", " not in canonical_json(value)


def test_canonical_json_rejects_float() -> None:
    # A float anywhere raises the canonical DomainValidationError (10-wp).
    for value in ({"a": 1.5}, [1, [2.5]], {"a": {"b": [3.25]}}, 0.5):
        with pytest.raises(
            DomainValidationError, match="floats are not allowed in hashed payloads"
        ):
            canonical_json(value)
    # Ints are fine — the error message tells callers to use int or str.
    assert canonical_json({"a": 1, "b": [2], "c": None}) == '{"a":1,"b":[2],"c":null}'


def test_payload_hash_stable_across_key_order() -> None:
    first = {"x": 1, "y": {"z": "s", "w": 2}}
    second = {"y": {"w": 2, "z": "s"}, "x": 1}
    assert payload_hash(first) == payload_hash(second)
    digest = payload_hash(first)
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_payload_hash_nfc_normalized() -> None:
    # é composed vs decomposed → same canonical form, same hash (10-wp).
    composed = {"s": "café", "t": ["résumé", {"n": "naïve"}]}
    decomposed = {"s": "cafe\u0301", "t": ["re\u0301sume\u0301", {"n": "nai\u0308ve"}]}
    assert canonical_json(composed) == canonical_json(decomposed)
    assert payload_hash(composed) == payload_hash(decomposed)
    assert "cafe\u0301" not in canonical_json(composed)


def test_plan_hash_is_payload_hash_over_plan_fields() -> None:
    # 01 §7.1: plan_hash = payload_hash({audience, channel, template_id,
    # allowed_fields, cadence_minutes}).
    fields = {"state": "record_state", "next_update_eta": "schedule"}
    assert plan_hash(
        audience="customer",
        channel="email",
        template_id="tpl_incident_update",
        allowed_fields=fields,
        cadence_minutes=15,
    ) == payload_hash(
        {
            "audience": "customer",
            "channel": "email",
            "template_id": "tpl_incident_update",
            "allowed_fields": fields,
            "cadence_minutes": 15,
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tags": {"a"}},
        {"blob": b"x"},
        {"when": datetime(2026, 1, 1, tzinfo=UTC)},
        {"nested": [{1: "int key"}]},
    ],
)
def test_canonical_json_rejects_non_json_values(payload: dict[str, object]) -> None:
    """01 §7.1: non-JSON values fail as DomainValidationError, never a raw TypeError."""
    with pytest.raises(DomainValidationError):
        canonical_json(payload)


def test_canonical_json_accepts_null() -> None:
    assert canonical_json({"a": None, "b": [True, 1, "s"]}) == '{"a":null,"b":[true,1,"s"]}'
