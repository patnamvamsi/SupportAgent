"""What the API needs from the composition root (``bootstrap.build_api_context``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from supportagent.app.approvals import ApprovalService
from supportagent.app.comms import CommsPlans
from supportagent.app.graph_review import GraphReview
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.queries import AdminQueries
from supportagent.app.replay import ReplayView
from supportagent.app.request_review import RequestReview
from supportagent.app.setup import SetupService
from supportagent.app.stats import StatsService
from supportagent.app.webhooks import WebhookService
from supportagent.domain.promotion import PromotionGate
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
    comms_plans: Callable[[], CommsPlans]
    webhooks: Callable[[], WebhookService]
    tenant_id: str
    identity: IdentityProvider
    session_secret: str
    secure_cookies: bool
    config_marker: str
    clock: Clock
    promotion_gate: PromotionGate = field(default_factory=PromotionGate)
    session_ttl: timedelta = timedelta(hours=12)
    login_ttl: timedelta = timedelta(minutes=10)
    idp_label: str = "your identity provider"
    #: ADR-0015 (WP-O3b): the system graph under review; ``None`` in tests that don't need it.
    graph_review: Callable[[], GraphReview] | None = None
    request_review: Callable[[], RequestReview] | None = None
