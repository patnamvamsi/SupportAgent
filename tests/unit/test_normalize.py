"""``normalize`` (01 §7.2) — the seven-step pipeline, idempotent.

Golden-table rows exercise each step of 01 §7.2 in order (UUID →
timestamp → IP → hex → tenant constants → numbers → whitespace/case/
truncation). Property test: ``normalize(normalize(x)) == normalize(x)``.
"""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from supportagent.domain.normalize import normalize


@pytest.mark.parametrize(
    ("text", "consts", "expected"),
    [
        # Step 1: UUID.
        pytest.param(
            "error 123e4567-e89b-12d3-a456-426614174000 in svc",
            (),
            "error <uuid> in svc",
            id="uuid",
        ),
        # Step 2: ISO-8601 and common log timestamps.
        pytest.param(
            "failed at 2024-05-01T10:30:45Z and 2024-05-01 09:15",
            (),
            "failed at <ts> and <ts>",
            id="timestamps",
        ),
        # Step 3: IPv4 with and without port.
        pytest.param(
            "conn from 10.0.0.1:8080 and 192.168.1.10",
            (),
            "conn from <ip> and <ip>",
            id="ip",
        ),
        # Step 4: 0x-prefixed and bare 8+-digit hex.
        pytest.param(
            "addr 0xdeadbeef frame deadbeef01",
            (),
            "addr <hex> frame <hex>",
            id="hex",
        ),
        # Step 5: tenant constants, literal and escaped, longest first —
        # "prod" alone must not carve into "prod-db-01".
        pytest.param(
            "db prod-db-01 down",
            ("prod", "prod-db-01"),
            "db <const> down",
            id="tenant-constants-longest-first",
        ),
        # Step 6: 3+-digit numbers; 1-2-digit numbers survive.
        pytest.param(
            "order 4521 failed after 42 retries",
            (),
            "order <n> failed after 42 retries",
            id="numbers",
        ),
        # Step 7: collapse whitespace, strip, lowercase.
        pytest.param(
            "  Mixed   CASE\n\ttext",
            (),
            "mixed case text",
            id="whitespace-case",
        ),
        # Step 7: truncate to 512 chars.
        pytest.param(
            "ab " * 300,
            (),
            ("ab " * 300).strip()[:512],
            id="truncate-512",
        ),
    ],
)
def test_normalize_golden_table(text: str, consts: tuple[str, ...], expected: str) -> None:
    assert normalize(text, consts) == expected


_TEXT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .:-_/"
_CONST_ALPHABET = "abcdefgxyzACME"


@given(
    text=st.text(alphabet=_TEXT_ALPHABET, max_size=120),
    consts=st.lists(st.text(alphabet=_CONST_ALPHABET, min_size=1, max_size=12), max_size=3),
)
# Constant substitution (step 5) runs after steps 1-4 and its "<const>"
# token creates a fresh word boundary, exposing hex/IP text to a second pass.
@example(text="deadbeefACME", consts=["ACME"])
@example(text="ACME10.0.0.1", consts=["ACME"])
def test_normalize_idempotent(text: str, consts: list[str]) -> None:
    """01 §7.2 property: normalize(normalize(x)) == normalize(x)."""
    once = normalize(text, consts)
    assert normalize(once, consts) == once
