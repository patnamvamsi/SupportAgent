"""What the API needs from the composition root (``bootstrap.build_api_context``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from supportagent.app.approvals import ApprovalService
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.queries import AdminQueries
from supportagent.app.replay import ReplayView
from supportagent.app.setup import SetupService
from supportagent.app.stats import StatsService
from supportagent.ports.clock import Clock
from supportagent.ports.identity import IdentityProvider


@dataclass(frozen=True)
class ApiContext:
    """Per-process wiring. Services are factories: one service (and UoW) per call."""

    setup: Callable[[], SetupService]
    approvals: Callable[[], ApprovalService]
    queries: Callable[[], AdminQueries]
    stats: Callable[[], StatsService]
    replay: Callable[[str], ReplayView]
    lifecycle: Callable[[], LifecycleService]
    tenant_id: str
    identity: IdentityProvider
    session_secret: str
    secure_cookies: bool
    config_marker: str
    clock: Clock
    session_ttl: timedelta = timedelta(hours=12)
    login_ttl: timedelta = timedelta(minutes=10)
    idp_label: str = "your identity provider"
