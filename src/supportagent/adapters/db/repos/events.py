"""Event log rows (03 §2.4): the append-only audit trail (INV-03).

``SqlEventSink`` satisfies the ``EventSink`` (append) and ``EventRepository``
(trace_events) protocols from ``supportagent.ports.events``: writes happen
inside the caller's UoW — committed or rolled back with the state they
describe (01 §3) — and the ``trg_events_append_only`` trigger (0002) makes
the row immutable at the database level.

Payloads are encrypted before they touch the wire (03 §2.4, §2.6): the
cipher seam below is satisfied structurally by
``supportagent.adapters.crypto.aead.Aead`` — the two adapter packages never
import each other (``adapters-independent`` contract, 01 §2), mirroring the
``KeyStore`` seam in ``blobs.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

import sqlalchemy as sa

from supportagent.adapters.db.tables import events
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import canonical_json
from supportagent.domain.model.event import Event

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PayloadCipher(Protocol):
    """Sealed-payload boundary for event rows (03 §2.6).

    Structural implementation: ``supportagent.adapters.crypto.aead.Aead``
    (the two adapter packages never import each other).
    """

    def encrypt(self, tenant_id: str, plaintext: bytes) -> bytes:
        """Return the sealed envelope for ``plaintext``."""
        ...

    def decrypt(self, tenant_id: str, envelope: bytes) -> bytes:
        """Open ``envelope``; raises on any failure (03 §3, PR flag)."""
        ...


class SqlEventSink:
    """``EventSink`` + ``EventRepository`` on SQL (03 §2.4, 01 §3).

    ``append`` is append-only: the row is written in the caller's UoW and
    the trigger forbids UPDATE; DELETE is reserved for the retention sweep
    (03 §2.4) and has no API here.
    """

    def __init__(self, *, uow: SqlUnitOfWork, cipher: PayloadCipher) -> None:
        self._uow = uow
        self._cipher = cipher

    def append(self, event: Event) -> None:
        """Write one event inside the caller's UoW transaction (01 §3)."""
        plaintext = canonical_json(event.payload).encode("utf-8")
        envelope = self._cipher.encrypt(event.tenant_id, plaintext)
        self._uow.connection.execute(
            sa.insert(events).values(
                id=event.id,
                tenant_id=event.tenant_id,
                trace_id=event.trace_id,
                span_id=event.span_id,
                parent_span_id=event.parent_span_id,
                kind=event.kind.value,
                actor=event.actor,
                model=event.model,
                tokens_in=event.tokens_in,
                tokens_out=event.tokens_out,
                latency_ms=event.latency_ms,
                status=event.status,
                payload_enc=envelope,
                ts_ms=int(event.ts.timestamp() * 1000),
            )
        )

    def trace_events(self, trace_id: str, tenant_id: str | None = None) -> list[Event]:
        """All events of one trace, decrypted, ordered by ``(ts_ms, span_id)`` (01 §3)."""
        rows = self._uow.connection.execute(
            sa.select(
                events.c.id,
                events.c.tenant_id,
                events.c.trace_id,
                events.c.span_id,
                events.c.parent_span_id,
                events.c.kind,
                events.c.actor,
                events.c.model,
                events.c.tokens_in,
                events.c.tokens_out,
                events.c.latency_ms,
                events.c.status,
                events.c.payload_enc,
                events.c.ts_ms,
            )
            .where(
                events.c.trace_id == trace_id,
                # BL-015: a tenant only ever replays its own events.
                sa.true() if tenant_id is None else events.c.tenant_id == tenant_id,
            )
            .order_by(events.c.ts_ms, events.c.span_id)
        ).all()
        out: list[Event] = []
        for row in rows:
            (
                event_id,
                tenant_id,
                event_trace_id,
                span_id,
                parent_span_id,
                kind,
                actor,
                model,
                tokens_in,
                tokens_out,
                latency_ms,
                status,
                payload_enc,
                ts_ms,
            ) = row
            plaintext = self._cipher.decrypt(tenant_id, bytes(payload_enc))
            out.append(
                Event(
                    id=event_id,
                    tenant_id=tenant_id,
                    trace_id=event_trace_id,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    kind=EventKind(kind),
                    actor=actor,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                    status=status,
                    payload=json.loads(plaintext.decode("utf-8")),
                    ts=_EPOCH + timedelta(milliseconds=ts_ms),
                )
            )
        return out
