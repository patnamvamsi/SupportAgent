"""WP-224: detectors evaluated by the instance (ADR-0023 §4).

Canonical test IDs from 12-wp WP-224: ``test_enabled_detector_evaluated_internally``,
``test_firing_detector_becomes_alert_event``, ``test_detector_not_double_fired``.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.system.ids import UlidIdGenerator
from supportagent.app.detector_eval import SOURCE
from supportagent.app.detector_review import DetectorReview, DetectorReviewStore
from supportagent.bootstrap import detector_evaluator_for
from supportagent.domain.errors import ToolError
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.signal_detectors import SignalDetector
from tests.unit.api_support import TENANT
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

_KEY = b"test" * 8


def _detector(cls: str, subject: str, expr: str, for_s: int = 0) -> SignalDetector:
    return SignalDetector(
        id=f"{cls.replace('_', '-')}/{subject}",
        cls=cls,  # type: ignore[arg-type]
        subject=subject,
        engine="prometheus" if cls != "log_silence" else "loki",
        expr=expr,
        for_s=for_s,
        severity="critical",
        summary=f"{subject}: {cls}",
    )


_DOWN = _detector("container_down", "api", 'time() - container_last_seen{service="api"} > 120', 60)
_SILENT = _detector("log_silence", "api", 'absent_over_time({service="api"}[110m])')
_LOOP = _detector(
    "crash_loop", "api", 'changes(container_start_time_seconds{service="api"}[30m]) >= 3'
)


class _Signals:
    """Prometheus and Loki now: the queries whose condition holds return a series."""

    def __init__(self) -> None:
        self.holding: set[str] = set()
        self.down = False
        self.asked: list[str] = []

    def instant(self, engine: str, expr: str) -> list[dict[str, str]]:
        self.asked.append(expr)
        if self.down:
            raise ToolError("prometheus unreachable")  # noqa: TRY003
        return [{"service": "api"}] if expr in self.holding else []


def _setup(db: Any, clock: Any) -> tuple[Any, _Signals, DetectorReview]:
    config = _shadow_config()
    config = config.model_copy(update={"tenant": config.tenant.model_copy(update={"id": TENANT})})
    review = DetectorReview(
        DetectorReviewStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT), clock
    )
    review.save_measurement([_DOWN, _SILENT, _LOOP], [], system="shop", days=7)
    review.decide([_DOWN.id, _SILENT.id], actor="ada")
    review.decide([_DOWN.id, _SILENT.id, _LOOP.id], actor="ada", mode="exported")
    signals = _Signals()
    evaluator = detector_evaluator_for(
        config, db, clock, ids=UlidIdGenerator(), master_key=_KEY, query=signals
    )
    return evaluator, signals, review


def _queued(db: Any) -> list[AlertEvent]:
    """What the evaluator put into the webhook inbox, decrypted as the drainer would."""
    from supportagent.adapters.crypto.aead import Aead  # noqa: PLC0415
    from supportagent.adapters.db.repos.blobs import SqlKeyStore  # noqa: PLC0415
    from supportagent.adapters.db.repos.webhooks import WebhookInbox  # noqa: PLC0415
    from supportagent.adapters.system.clock import SystemClock  # noqa: PLC0415

    uow = SqlUnitOfWork(db)
    inbox = WebhookInbox(uow, Aead(master_key=_KEY, keys=SqlKeyStore(uow), clock=SystemClock()))
    with uow:
        rows = inbox.pending(TENANT, limit=50)
    return [AlertEvent.model_validate_json(body) for _, body in rows]


def test_enabled_detector_evaluated_internally(db: Any, clock: Any) -> None:
    evaluator, signals, review = _setup(db, clock)
    assert evaluator.run_once() == 0
    assert sorted(signals.asked) == sorted([_DOWN.expr, _SILENT.expr]), "internal ones only"
    assert {d.id for d in review.enabled("exported")} == {_LOOP.id}, "rule files, not evaluated"
    state = evaluator.state()
    assert state[_DOWN.id]["checked"] and state[_DOWN.id]["fired"] is None


def test_firing_detector_becomes_alert_event(db: Any, clock: Any) -> None:
    evaluator, signals, _ = _setup(db, clock)
    signals.holding = {_DOWN.expr, _SILENT.expr}
    assert evaluator.run_once() == 1, "silence fires at once; the container waits 60 s"
    clock.advance(60)
    assert evaluator.run_once() == 1, "the container has been gone for its 60 s"
    events = _queued(db)
    assert [e.alert_type for e in events] == ["LogSilence", "ContainerDown"]
    down = events[1]
    assert down.raw_ref.startswith(f"webhook:{SOURCE}/") and down.alert_status == "firing"
    assert down.resource == "api" and down.severity_hint == "P1"
    assert down.external_id == f"ContainerDown/detector-{_DOWN.id}", "one fingerprint per detector"

    signals.holding = set()
    clock.advance(60)
    assert evaluator.run_once() == 2
    assert [e.alert_status for e in _queued(db)[2:]] == ["resolved", "resolved"]


def test_detector_not_double_fired(db: Any, clock: Any) -> None:
    evaluator, signals, review = _setup(db, clock)
    signals.holding = {_SILENT.expr}
    assert evaluator.run_once() == 1
    for _ in range(3):
        clock.advance(60)
        assert evaluator.run_once() == 0, "still firing: no second alert"
    signals.down = True
    clock.advance(60)
    assert evaluator.run_once() == 0, "a monitoring outage is neither failure nor recovery"
    assert evaluator.state()[_SILENT.id]["error"] == "prometheus unreachable"
    assert evaluator.state()[_SILENT.id]["fired"] is not None, "still firing"
    signals.down = False

    review.decide([_DOWN.id], actor="ada")  # turned off while firing
    clock.advance(60)
    assert evaluator.run_once() == 1
    resolved = _queued(db)[-1]
    assert resolved.alert_status == "resolved"
    assert resolved.external_id == f"LogSilence/detector-{_SILENT.id}"
    clock.advance(60)
    assert evaluator.run_once() == 0
    with db.begin() as conn:
        rows = conn.execute(sa.text("SELECT COUNT(*) FROM webhook_inbox")).scalar()
    assert rows == 2
