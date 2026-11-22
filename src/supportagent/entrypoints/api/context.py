"""What the API needs from the composition root (``bootstrap.build_api_context``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from supportagent.app.approvals import ApprovalService
from supportagent.app.comms import CommsPlans
from supportagent.app.component_summaries import ComponentSummaryStore
from supportagent.app.detector_review import DetectorReview
from supportagent.app.graph_review import GraphReview
from supportagent.app.lifecycle import LifecycleService
from supportagent.app.onboarding_control import OnboardingControl
from supportagent.app.queries import AdminQueries
from supportagent.app.readiness_store import OnboardingStatusStore, ReadinessStore
from supportagent.app.replay import ReplayView
from supportagent.app.request_review import RequestReview
from supportagent.app.setup import SetupService
from supportagent.app.stats import StatsService
from supportagent.app.trace_answer import TraceAnswer
from supportagent.app.tracing import TraceRun
from supportagent.app.webhooks import WebhookService
from supportagent.domain.model.flow import Flow
from supportagent.domain.promotion import PromotionGate
from supportagent.ports.clock import Clock
from supportagent.ports.diagnostics import SchemaProbe
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
    #: WP-O6b: the system's last readiness report.
    readiness: Callable[[], ReadinessStore] | None = None
    #: ADR-0017 (WP-A4): the last ``onboard run``'s steps and needs.
    onboarding_status: Callable[[], OnboardingStatusStore] | None = None
    #: ADR-0023 §2 (WP-223): the stored manifest and the autopilot's run requests.
    onboarding_control: Callable[[], OnboardingControl] | None = None
    #: ADR-0023 §3 (WP-222): each component's summary, written by onboarding.
    component_summaries: Callable[[], ComponentSummaryStore] | None = None
    #: ADR-0023 §2 (WP-223): measured detectors, and which ones a person enabled.
    detector_review: Callable[[], DetectorReview] | None = None
    #: ADR-0023 §4 (WP-224): each evaluated detector's state, by id.
    detector_watches: Callable[[], Mapping[str, Mapping[str, object]]] | None = None
    #: ADR-0017 (WP-A6): the tenant's onboarding events, newest first.
    onboarding_history: Callable[[], list[dict[str, object]]] | None = None
    #: ADR-0017 (WP-A6): which calibration triage scores with (``fitted`` or
    #: ``artifact``), and why.
    calibration_in_use: Callable[[], tuple[str, str]] | None = None
    #: ADR-0021: the reviewed flows, and one trace (flow, object id, who asks).
    flows: Callable[[], Mapping[str, Flow]] | None = None
    trace: Callable[[str, str, str], tuple[TraceRun, TraceAnswer | None]] | None = None
    #: 02 §2 ``/readyz``: the database's applied revision against this build's head.
    schema: SchemaProbe | None = None
