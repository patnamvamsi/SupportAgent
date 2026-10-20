"""SystemClock adapter (01 §3): real wall-clock time, aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """The production clock (01 §3)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class ReplayClock:
    """A clock pinned to a moment that only moves when told (``shadow import``).

    ``shadow import`` replays partner history with the clock set to each
    record's ``opened_at`` (10-wp WP-014), so counterfactual timestamps land in
    the incident's own timeline rather than at import time.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when
