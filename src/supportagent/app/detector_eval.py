"""Detectors evaluated by the instance (ADR-0023 §4, WP-224).

The ``detectors.evaluate`` worker job runs every enabled ``internal`` detector's
query against the deployment's Prometheus or Loki, read-only, with the access
onboarding measured them with. A detector that starts or stops firing sends one
Alertmanager-shaped alert into the webhook inbox, where the drainer triages it
like any alert (``domain.detector_eval``). Detectors in ``exported`` mode are
rendered into rule files instead and never evaluated here, so nothing fires
twice.

Each detector's state (pending since, fired at, last checked, last error) is
kept per tenant, so a restart neither re-fires nor forgets a firing alert.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import TracebackType
from typing import Any, Protocol, Self

from supportagent.app.detector_review import DetectorReview
from supportagent.app.readiness_store import TenantDocument
from supportagent.domain.alertmanager import DEFAULT_SERVICE_LABELS, map_alertmanager
from supportagent.domain.detector_eval import Watch, alert, observe
from supportagent.domain.errors import ExternalServiceError
from supportagent.domain.ids import IdPrefix
from supportagent.domain.signal_detectors import SignalDetector
from supportagent.ports.clock import Clock
from supportagent.ports.idgen import IdGenerator
from supportagent.ports.signals import SignalQuery

#: The ``source`` of the alerts the instance's own detectors send.
SOURCE = "detectors"


class DetectorWatchStore(TenantDocument):
    table, column = "detector_watch", "watch_json"


class _UoW(Protocol):
    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _Inbox(Protocol):
    def add(
        self, *, id: str, tenant_id: str, source: str, event_json: str, now_ts: int
    ) -> None: ...


class AlertQueue:
    """Alertmanager-shaped alerts into the webhook inbox, mapped as a delivery would be."""

    def __init__(
        self, *, uow: _UoW, inbox: _Inbox, ids: IdGenerator, clock: Clock, tenant_id: str
    ) -> None:
        self._uow = uow
        self._inbox = inbox
        self._ids = ids
        self._clock = clock
        self._tenant = tenant_id

    def __call__(self, alerts: Sequence[Mapping[str, object]]) -> int:
        now = self._clock.now()
        events = map_alertmanager(
            {"alerts": list(alerts)},
            tenant_id=self._tenant,
            source=SOURCE,
            received_at=now,
            service_labels=DEFAULT_SERVICE_LABELS,
        )
        with self._uow:
            for evt in events:
                self._inbox.add(
                    id=self._ids.new(IdPrefix.ALERT),
                    tenant_id=self._tenant,
                    source=SOURCE,
                    event_json=evt.model_dump_json(),
                    now_ts=int(now.timestamp()),
                )
            self._uow.commit()
        return len(events)


class DetectorEvaluator:
    def __init__(
        self,
        *,
        review: Callable[[], DetectorReview],
        watches: DetectorWatchStore,
        query: SignalQuery,
        deliver: Callable[[Sequence[Mapping[str, object]]], int],
        clock: Clock,
    ) -> None:
        self._review = review
        self._watches = watches
        self._query = query
        self._deliver = deliver
        self._clock = clock

    def state(self) -> dict[str, Any]:
        found = (self._watches.load() or {}).get("watches", {})
        return found if isinstance(found, dict) else {}

    def run_once(self) -> int:
        """Evaluate every enabled internal detector once; returns the alerts sent."""
        now = int(self._clock.now().timestamp())
        enabled = {d.id: d for d in self._review().enabled("internal")}
        before = self.state()
        after: dict[str, dict[str, Any]] = {}
        alerts: list[Mapping[str, object]] = []
        for det in enabled.values():
            raw = before.get(det.id, {})
            watch = Watch(raw.get("since"), raw.get("fired"))
            entry = {"subject": det.subject, "summary": det.summary, "checked": now}
            try:
                holds = bool(self._query.instant(det.engine, det.expr))
            except ExternalServiceError as exc:
                after[det.id] = dict(raw) | entry | {"error": str(exc)[:200]}
                continue
            nxt, transition = observe(watch, holds, now, det.for_s)
            if transition is not None:
                alerts.append(alert(det, transition, fired=watch.fired or now, now=now))
            after[det.id] = entry | {"since": nxt.since, "fired": nxt.fired, "error": None}
        for gone, raw in before.items():  # turned off (or re-measured away) while firing
            if gone not in enabled and raw.get("fired") is not None:
                det = _stand_in(gone, raw)
                alerts.append(alert(det, "resolve", fired=int(raw["fired"]), now=now))
        if alerts:
            self._deliver(alerts)
        self._watches.save({"watches": after, "evaluated_ts": now})
        return len(alerts)


def _stand_in(detector_id: str, raw: Mapping[str, Any]) -> SignalDetector:
    """Enough of a detector no longer enabled to resolve its alert."""
    cls = detector_id.split("/", 1)[0].replace("-", "_")
    return replace(
        SignalDetector(
            id=detector_id,
            cls="container_down",
            subject=str(raw.get("subject", "")),
            engine="prometheus",
            expr="",
            for_s=0,
            severity="critical",
            summary=str(raw.get("summary", "")),
        ),
        cls=cls,  # type: ignore[arg-type]
    )


__all__ = ["SOURCE", "AlertQueue", "DetectorEvaluator", "DetectorWatchStore"]
