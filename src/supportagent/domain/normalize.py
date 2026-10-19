"""String normalization for case fingerprints (01 §7.2).

``normalize(text, tenant_constants)`` strips the tenant-unique noise that
would otherwise split one incident into many cases (INV-02): UUIDs,
timestamps, IPs, hex runs, tenant constants and 3+-digit numbers. Applied
in a fixed order (01 §7.2), all patterns case-insensitive:

1. UUID → ``<uuid>``
2. ISO-8601 and common log timestamps → ``<ts>``
3. IPv4 (optional port) → ``<ip>``
4. ``0x``-prefixed or 8+-digit hex → ``<hex>``
5. tenant constants (literal, escaped, longest first) → ``<const>``
6. 3+-digit numbers → ``<n>``
7. collapse whitespace to one space, strip, lowercase, truncate to 512.

Strings are NFC-normalized first (01 §7.1). The pipeline is idempotent —
``normalize(normalize(x)) == normalize(x)``, required by the 01 §7.2
property test. This holds even when a tenant constant is a substring of a
placeholder token (e.g. ``"c"`` inside ``<const>``): placeholder tokens are
inert, so step 5 is applied only outside of them (see ``_PLACEHOLDER``);
the other steps are idempotent on their own because the placeholders
contain no digits.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

#: 512-char cap (01 §7.2 step 7).
MAX_LEN = 512

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?", re.IGNORECASE
)
_IP = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?\b", re.IGNORECASE)
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUM = re.compile(r"\b\d{3,}\b", re.IGNORECASE)
_WS = re.compile(r"\s+")

#: Placeholder tokens the pipeline emits (``<uuid>``, ``<ts>``, ``<ip>``,
#: ``<hex>``, ``<const>``). They are inert: step 5 must not substitute
#: inside them, or the pipeline would not be idempotent (e.g. constant
#: ``"c"`` would re-match inside ``<const>`` on a second pass).
_PLACEHOLDER = re.compile(r"(<[a-z]+>)")


def _constants(s: str, consts: Sequence[str]) -> str:
    """Step 5: tenant constants (literal, escaped, longest first).

    Applied only outside placeholder tokens (``_PLACEHOLDER``) so a
    constant that is a substring of ``<const>`` et al. cannot re-match on
    a later pass — the idempotency property of 01 §7.2.
    """
    if not any(consts):
        return s
    ordered = sorted((c for c in consts if c), key=len, reverse=True)
    pieces = _PLACEHOLDER.split(s)
    for i in range(0, len(pieces), 2):
        for const in ordered:
            pieces[i] = re.sub(re.escape(const), "<const>", pieces[i], flags=re.IGNORECASE)
    return "".join(pieces)


def normalize(text: str, tenant_constants: Sequence[str] = ()) -> str:
    """Apply the 01 §7.2 pipeline to ``text``; see the module docstring."""
    s = unicodedata.normalize("NFC", text)
    s = _UUID.sub("<uuid>", s)
    s = _TS.sub("<ts>", s)
    s = _IP.sub("<ip>", s)
    s = _HEX.sub("<hex>", s)
    s = _constants(s, tenant_constants)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip().lower()
    return s[:MAX_LEN]
