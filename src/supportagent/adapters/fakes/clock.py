"""FrozenClock test double (04 §3): deterministic time for unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta


class FrozenClock:
    """A clock that only moves when the test advances it (04 §3)."""

    def __init__(self, *, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the frozen clock forward by ``seconds`` (04 §3)."""
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        """Jump to ``when`` (shadow import replays history at each record's time)."""
        self._now = when
