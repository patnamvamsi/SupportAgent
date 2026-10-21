"""``retention.sweep`` (02 §7, 03 §2.4; BL-028): delete rows past their retention.

The windows are config ``retention`` (``events_days``, ``alerts_days``,
``comms_days``). The summary is one ``decision`` event with
``stage: retention`` and row counts only: 03 §2.4 asks for a ``retention``
summary event, but the 20 event kinds are fixed (ARCH §15), so the
summary rides on ``decision`` instead of adding a kind. It's written after
the delete in the same transaction, so it survives its own sweep.
"""

from __future__ import annotations

from datetime import timedelta
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.event import Event
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _Purge(Protocol):
    def purge(
        self,
        tenant_id: str,
        *,
        events_before_ms: int,
        alerts_before_ts: int,
        comms_before_ts: int,
    ) -> dict[str, int]: ...


class RetentionService:
    def __init__(  # noqa: PLR0913 — the ports plus the three windows
        self,
        *,
        uow: _UoW,
        store: _Purge,
        events: EventSink,
        ids: IdGenerator,
        clock: Clock,
        tenant_id: str,
        events_days: int,
        alerts_days: int,
        comms_days: int,
    ) -> None:
        self._uow = uow
        self._store = store
        self._events = events
        self._ids = ids
        self._clock = clock
        self._tenant = tenant_id
        self._windows = (events_days, alerts_days, comms_days)

    def sweep(self) -> dict[str, int]:
        now = self._clock.now()
        events_days, alerts_days, comms_days = self._windows
        with self._uow:
            counts = self._store.purge(
                self._tenant,
                events_before_ms=int((now - timedelta(days=events_days)).timestamp() * 1000),
                alerts_before_ts=int((now - timedelta(days=alerts_days)).timestamp()),
                comms_before_ts=int((now - timedelta(days=comms_days)).timestamp()),
            )
            event_id = self._ids.new(IdPrefix.EVENT)
            self._events.append(
                Event(
                    id=event_id,
                    tenant_id=self._tenant,
                    trace_id=f"retention-{now:%Y%m%d}",
                    span_id=f"retention_{event_id}",
                    parent_span_id=None,
                    kind=EventKind.DECISION,
                    actor="system",
                    payload={"v": 1, "stage": "retention", "deleted": counts},
                    ts=now,
                )
            )
            self._uow.commit()
        return counts


__all__ = ["RetentionService"]
