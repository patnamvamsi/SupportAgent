"""Clock port (01 §3): time is injected, never read inside the domain."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Produces the current time."""

    def now(self) -> datetime:
        """The current time, always timezone-aware UTC."""
        ...
