"""Generic webhooks (ARCH §7, 02 §2; 11-wp WP-113): verify, map, queue; drain later.

``WebhookService.receive(source, headers, body)`` accepts a delivery only if
all of these hold:

1. the source is configured (the allowlist);
2. ``X-SA-Signature`` is the HMAC of ``X-SA-Timestamp + "." + body`` under
   the source's secret, and the timestamp is within 5 minutes;
3. ``X-SA-Nonce`` hasn't been used for this source (single-use);
4. the JSON body maps to an ``AlertEvent`` through the source's mapping.

An accepted delivery goes into the encrypted inbox in the same unit of work
as its nonce, and the request returns at once. A rejected one writes a
``webhook_rejected`` event (reason, source; never the body) and raises
``WebhookRejected``.

``WebhookDrainer.run_once`` is the worker side: it triages each queued event
through the configured runner (the shadow runner in shadow mode) and marks
it processed. A failure leaves the row queued for the next run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

from supportagent.domain.events import EventKind
from supportagent.domain.ids import IdPrefix
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.event import Event
from supportagent.domain.webhook import (
    NONCE_TTL_S,
    WebhookRejected,
    check_signature,
    map_alert,
)
from supportagent.ports.clock import Clock
from supportagent.ports.events import EventSink
from supportagent.ports.idgen import IdGenerator

_log = logging.getLogger("supportagent.webhooks")
_MAX_BODY = 1_000_000


class _UoW(Protocol):
    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class _Nonces(Protocol):
    def claim(
        self, tenant_id: str, source: str, nonce: str, *, now_ts: int, ttl_s: int
    ) -> bool: ...


class _Inbox(Protocol):
    def add(
        self, *, id: str, tenant_id: str, source: str, event_json: str, now_ts: int
    ) -> None: ...

    def pending(self, tenant_id: str, *, limit: int) -> list[tuple[str, str]]: ...

    def mark(self, item_id: str, *, now_ts: int, error: str | None = None) -> None: ...


class _Runner(Protocol):
    def run(self, evt: AlertEvent) -> object: ...


@dataclass(frozen=True, slots=True)
class WebhookSource:
    secret: bytes
    mapping: Mapping[str, str]


class WebhookService:
    def __init__(  # noqa: PLR0913 — the ports plus the configured sources
        self,
        *,
        uow: _UoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        nonces: _Nonces,
        inbox: _Inbox,
        sources: Mapping[str, WebhookSource],
        tenant_id: str,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._nonces = nonces
        self._inbox = inbox
        self._sources = dict(sources)
        self._tenant = tenant_id

    def receive(self, source: str, headers: Mapping[str, str], body: bytes) -> str:
        """The inbox id of the accepted delivery; ``WebhookRejected`` otherwise."""
        try:
            return self._accept(source, {k.lower(): v for k, v in headers.items()}, body)
        except WebhookRejected as exc:
            self._reject(source, exc)
            raise

    def _accept(self, source: str, headers: Mapping[str, str], body: bytes) -> str:
        config = self._sources.get(source)
        if config is None:
            raise WebhookRejected("unknown_source")
        now = self._clock.now()
        check_signature(
            config.secret,
            timestamp=headers.get("x-sa-timestamp", ""),
            body=body,
            header=headers.get("x-sa-signature", ""),
            now=now,
        )
        nonce = headers.get("x-sa-nonce", "")
        if not nonce or len(nonce) > 128:  # noqa: PLR2004
            raise WebhookRejected("replayed_nonce", "X-SA-Nonce is required (1-128 chars)")
        if len(body) > _MAX_BODY:
            raise WebhookRejected("bad_payload", "body over 1 MB")
        try:
            doc = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookRejected("bad_payload", "body is not JSON") from exc
        evt = map_alert(doc, config.mapping, tenant_id=self._tenant, source=source, received_at=now)
        item_id = self._ids.new(IdPrefix.ALERT)
        now_ts = int(now.timestamp())
        with self._uow:
            if not self._nonces.claim(
                self._tenant, source, nonce, now_ts=now_ts, ttl_s=NONCE_TTL_S
            ):
                raise WebhookRejected("replayed_nonce")
            self._inbox.add(
                id=item_id,
                tenant_id=self._tenant,
                source=source,
                event_json=evt.model_dump_json(),
                now_ts=now_ts,
            )
            self._uow.commit()
        return item_id

    def _reject(self, source: str, exc: WebhookRejected) -> None:
        event_id = self._ids.new(IdPrefix.EVENT)
        with self._uow:
            self._events.append(
                Event(
                    id=event_id,
                    tenant_id=self._tenant,
                    trace_id=f"tr_{event_id}",
                    span_id="webhook",
                    parent_span_id=None,
                    kind=EventKind.WEBHOOK_REJECTED,
                    actor=f"webhook:{source}"[:64],
                    status="error",
                    payload={
                        "v": 1,
                        "source": source[:64],
                        "reason": exc.reason,
                        "detail": exc.detail[:200],
                    },
                    ts=self._clock.now(),
                )
            )
            self._uow.commit()


class WebhookDrainer:
    """``webhooks.drain`` (5 s): triage queued deliveries in order."""

    def __init__(
        self, *, uow: _UoW, inbox: _Inbox, runner: _Runner, clock: Clock, tenant_id: str
    ) -> None:
        self._uow = uow
        self._inbox = inbox
        self._runner = runner
        self._clock = clock
        self._tenant = tenant_id

    def run_once(self, limit: int = 50) -> int:
        with self._uow:
            items = self._inbox.pending(self._tenant, limit=limit)
        done = 0
        for item_id, event_json in items:
            try:
                self._runner.run(AlertEvent.model_validate_json(event_json))
            except Exception:  # noqa: BLE001 — any failure leaves the row queued
                _log.exception("webhook delivery %s failed; retrying next run", item_id)
                break
            with self._uow:
                self._inbox.mark(item_id, now_ts=int(self._clock.now().timestamp()))
                self._uow.commit()
            done += 1
        return done


__all__ = ["WebhookDrainer", "WebhookService", "WebhookSource"]
