"""IdGenerator port (01 §3): ids are injected, never minted in the domain."""

from __future__ import annotations

from typing import Protocol

from supportagent.domain.ids import IdPrefix


class IdGenerator(Protocol):
    """Mints new ids for one of the 13 prefixes (03 §1)."""

    def new(self, prefix: IdPrefix) -> str:
        """A fresh id: ``<prefix>_<ULID>`` (01 §3, 03 §1)."""
        ...
