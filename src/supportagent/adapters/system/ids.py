"""UlidIdGenerator adapter (01 §3, 03 §1): <prefix>_<26-char Crockford ULID>."""

from __future__ import annotations

from ulid import ULID

from supportagent.domain.ids import IdPrefix


class UlidIdGenerator:
    """The production id generator (01 §3)."""

    def new(self, prefix: IdPrefix) -> str:
        return f"{prefix.value}_{ULID()}"
