"""Webhooks (ARCH §7, 02 §2; WP-113, WP-213): verify, map, queue; drain later.

``WebhookService.receive(source, headers, body)`` accepts a delivery from a
``generic`` source only if all of these hold:

1. the source is configured (the allowlist);
2. ``X-SA-Signature`` is the HMAC of ``X-SA-Timestamp + "." + body`` under
   the source's secret, and the timestamp is within 5 minutes;
3. ``X-SA-Nonce`` hasn't been used for this source (single-use);
4. the JSON body maps to an ``AlertEvent`` through the source's mapping.

An ``alertmanager`` or ``grafana`` source (ADR-0022 §2) authenticates with
``Authorization: Bearer <secret>`` (what those tools can send), and its body
is a batch of alerts mapped by the preset in ``domain.alertmanager``: one
queued ``AlertEvent`` per alert. Those tools resend a firing alert on their
repeat interval by design, so there is no nonce: the open case's
fingerprint (INV-02) absorbs the repeats.

An accepted delivery goes into the encrypted inbox in the same unit of work
as its nonce, and the request returns at once. A rejected one writes a
``webhook_rejected`` event (reason, source; never the body) and raises
``WebhookRejected``.

``WebhookDrainer.run_once`` is the worker side: it triages each queued firing
event through the configured runner (the shadow runner in shadow mode) and
marks it processed. A resolved alert is never triaged: ``AlertResolutions``
records it on the open case holding its fingerprint (a ``sync`` event on the
case's timeline) and leaves the case's status alone, because the source
resolving an alert isn't a person resolving the incident. A failure leaves
the row queued for the next run.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.alertmanager import DEFAULT_SERVICE_LABELS, map_alertmanager
from supportagent.domain.events import EventKind
from supportagent.domain.fingerprint import fingerprint
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
    kind: str = "generic"
    service_labels: tuple[str, ...] = DEFAULT_SERVICE_LABELS


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

    def receive(self, source: str, headers: Mapping[str, str], body: bytes) -> list[str]:
        """The inbox ids of the accepted delivery; ``WebhookRejected`` otherwise."""
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            config = self._source(source)
            if config.kind == "generic":
                return [self._accept(source, config, lowered, body)]
            return self._accept_alerts(source, config, lowered, body)
        except WebhookRejected as exc:
            self._reject(source, exc)
            raise

    def _source(self, source: str) -> WebhookSource:
        config = self._sources.get(source)
        if config is None:
            raise WebhookRejected("unknown_source")
        return config

    def is_batch(self, source: str) -> bool:
        """True for an alerting-tool source, whose deliveries carry many alerts."""
        config = self._sources.get(source)
        return config is not None and config.kind != "generic"

    def _accept_alerts(
        self, source: str, config: WebhookSource, headers: Mapping[str, str], body: bytes
    ) -> list[str]:
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.strip().encode(), config.secret
        ):
            raise WebhookRejected("bad_signature", "bearer token missing or wrong")
        doc = _json(body)
        now = self._clock.now()
        events = map_alertmanager(
            doc,
            tenant_id=self._tenant,
            source=source,
            received_at=now,
            service_labels=config.service_labels,
        )
        now_ts = int(now.timestamp())
        ids: list[str] = []
        with self._uow:
            for evt in events:
                item_id = self._ids.new(IdPrefix.ALERT)
                self._inbox.add(
                    id=item_id,
                    tenant_id=self._tenant,
                    source=source,
                    event_json=evt.model_dump_json(),
                    now_ts=now_ts,
                )
                ids.append(item_id)
            self._uow.commit()
        return ids

    def _accept(
        self, source: str, config: WebhookSource, headers: Mapping[str, str], body: bytes
    ) -> str:
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
        doc = _json(body)
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


def _json(body: bytes) -> object:
    if len(body) > _MAX_BODY:
        raise WebhookRejected("bad_payload", "body over 1 MB")
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejected("bad_payload", "body is not JSON") from exc


class _Resolutions(Protocol):
    def record(self, evt: AlertEvent) -> bool: ...


class _ConnUoW(_UoW, Protocol):
    @property
    def connection(self) -> sa.Connection: ...


class AlertResolutions:
    """A resolved alert, noted on the open case holding its fingerprint (ADR-0022 §2).

    It never changes the case's status: the source resolving an alert is
    evidence for the case, not a person resolving the incident. Without an
    open case there is nothing to note.
    """

    def __init__(  # noqa: PLR0913 — the ports plus the fingerprint constants
        self,
        *,
        uow: _ConnUoW,
        clock: Clock,
        ids: IdGenerator,
        events: EventSink,
        tenant_id: str,
        tenant_constants: Sequence[str] = (),
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ids = ids
        self._events = events
        self._tenant = tenant_id
        self._consts = tuple(tenant_constants)

    def record(self, evt: AlertEvent) -> bool:
        """True when an open case got the note."""
        fp = fingerprint(evt, self._consts)
        with self._uow:
            row = (
                self._uow.connection.execute(
                    sa.text(
                        "SELECT id, status, trace_id FROM cases WHERE tenant_id = :tenant "
                        "AND fingerprint = :fp AND status <> 'resolved'"
                    ),
                    {"tenant": self._tenant, "fp": fp},
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            event_id = self._ids.new(IdPrefix.EVENT)
            self._events.append(
                Event(
                    id=event_id,
                    tenant_id=self._tenant,
                    trace_id=str(row["trace_id"]),
                    span_id=f"alert_resolved_{event_id}",
                    parent_span_id=None,
                    kind=EventKind.SYNC,
                    actor=evt.raw_ref.split("/", 1)[0][:64],
                    payload={
                        "v": 1,
                        "transition": "alert_resolved",
                        "case_id": str(row["id"]),
                        "case_status": str(row["status"]),
                        "external_id": evt.external_id[:255],
                        "note": "the source resolved the alert; the case stays as it is",
                    },
                    ts=self._clock.now(),
                )
            )
            self._uow.commit()
        return True


class WebhookDrainer:
    """``webhooks.drain`` (5 s): triage queued deliveries in order."""

    def __init__(  # noqa: PLR0913 — the ports plus the resolved-alert recorder
        self,
        *,
        uow: _UoW,
        inbox: _Inbox,
        runner: _Runner,
        clock: Clock,
        tenant_id: str,
        resolutions: _Resolutions | None = None,
    ) -> None:
        self._uow = uow
        self._inbox = inbox
        self._runner = runner
        self._clock = clock
        self._tenant = tenant_id
        self._resolutions = resolutions

    def _handle(self, evt: AlertEvent) -> None:
        if evt.alert_status == "resolved":
            if self._resolutions is not None:
                self._resolutions.record(evt)
            return
        self._runner.run(evt)

    def run_once(self, limit: int = 50) -> int:
        with self._uow:
            items = self._inbox.pending(self._tenant, limit=limit)
        done = 0
        for item_id, event_json in items:
            try:
                self._handle(AlertEvent.model_validate_json(event_json))
            except Exception:  # noqa: BLE001 — any failure leaves the row queued
                _log.exception("webhook delivery %s failed; retrying next run", item_id)
                break
            with self._uow:
                self._inbox.mark(item_id, now_ts=int(self._clock.now().timestamp()))
                self._uow.commit()
            done += 1
        return done


__all__ = ["AlertResolutions", "WebhookDrainer", "WebhookService", "WebhookSource"]
