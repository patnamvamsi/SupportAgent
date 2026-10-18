"""SystemClock adapter (01 §3): real wall-clock time, aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """The production clock (01 §3)."""

    def now(self) -> datetime:
        return datetime.now(UTC)
