"""What a deployment measures: its history (WP-215), and a detector's query now (WP-224)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from supportagent.domain.signal_detectors import Engine, Signals


class SignalHistoryReader(Protocol):
    def measure(self, components: Iterable[str], *, now: datetime, days: int) -> Signals:
        """The signals for ``components`` over the last ``days``, in fixed steps."""
        ...


class SignalQuery(Protocol):
    def instant(self, engine: Engine, expr: str) -> list[dict[str, str]]:
        """The labels of each series ``expr`` returns now (empty: the condition doesn't hold).

        Raises ``ExternalServiceError`` when the query can't run.
        """
        ...


__all__ = ["SignalHistoryReader", "SignalQuery"]
