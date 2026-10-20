"""What the API needs from the composition root (``bootstrap.build_api_context``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from supportagent.app.setup import SetupService
from supportagent.ports.clock import Clock
from supportagent.ports.identity import IdentityProvider


@dataclass(frozen=True)
class ApiContext:
    """Per-process wiring. ``setup`` is a factory: one service (and UoW) per request."""

    setup: Callable[[], SetupService]
    identity: IdentityProvider
    session_secret: str
    secure_cookies: bool
    config_marker: str
    clock: Clock
    session_ttl: timedelta = timedelta(hours=12)
    login_ttl: timedelta = timedelta(minutes=10)
