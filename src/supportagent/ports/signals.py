"""SignalHistoryReader: what a deployment measures, over its history (WP-215)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from supportagent.domain.signal_detectors import Signals


class SignalHistoryReader(Protocol):
    def measure(self, components: Iterable[str], *, now: datetime, days: int) -> Signals:
        """The signals for ``components`` over the last ``days``, in fixed steps."""
        ...


__all__ = ["SignalHistoryReader"]
