"""SequentialIdGenerator test double (04 §3): case_0001, case_0002, ..."""

from __future__ import annotations

from supportagent.domain.ids import IdPrefix


class SequentialIdGenerator:
    """Deterministic ids for unit tests (04 §3)."""

    def __init__(self) -> None:
        self._counters: dict[IdPrefix, int] = {}

    def new(self, prefix: IdPrefix) -> str:
        count = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = count
        return f"{prefix.value}_{count:04d}"
