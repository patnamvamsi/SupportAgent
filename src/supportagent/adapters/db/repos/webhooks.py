"""Webhook state (11-wp WP-113): single-use nonces and the encrypted inbox (03 §3).

``SqlNonceStore.claim`` inserts ``(tenant, source, nonce)`` with
``ON CONFLICT DO NOTHING`` and reports whether this call was the first, so
two concurrent deliveries of one nonce can't both win (ARCH §7 replay
protection). Expired nonces are purged on the way. The signature and window
checks are pure (``domain.webhook``).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from supportagent.adapters.db.repos.events import PayloadCipher
from supportagent.adapters.db.tables import webhook_inbox, webhook_nonces
from supportagent.adapters.db.uow import SqlUnitOfWork


class WebhookInbox:
    def __init__(self, uow: SqlUnitOfWork, cipher: PayloadCipher) -> None:
        self._uow = uow
        self._cipher = cipher

    def add(self, *, id: str, tenant_id: str, source: str, event_json: str, now_ts: int) -> None:
        self._uow.connection.execute(
            sa.insert(webhook_inbox).values(
                id=id,
                tenant_id=tenant_id,
                source=source,
                event_enc=self._cipher.encrypt(tenant_id, event_json.encode("utf-8")),
                received_ts=now_ts,
            )
        )

    def pending(self, tenant_id: str, *, limit: int) -> list[tuple[str, str]]:
        rows = self._uow.connection.execute(
            sa.select(webhook_inbox.c.id, webhook_inbox.c.event_enc)
            .where(webhook_inbox.c.tenant_id == tenant_id, webhook_inbox.c.processed_ts.is_(None))
            .order_by(webhook_inbox.c.received_ts, webhook_inbox.c.id)
            .limit(limit)
        ).all()
        return [
            (str(r[0]), self._cipher.decrypt(tenant_id, bytes(r[1])).decode("utf-8")) for r in rows
        ]

    def mark(self, item_id: str, *, now_ts: int, error: str | None = None) -> None:
        self._uow.connection.execute(
            sa.update(webhook_inbox)
            .where(webhook_inbox.c.id == item_id)
            .values(processed_ts=now_ts, error=error)
        )


class SqlNonceStore:
    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def claim(self, tenant_id: str, source: str, nonce: str, *, now_ts: int, ttl_s: int) -> bool:
        conn = self._uow.connection
        conn.execute(sa.delete(webhook_nonces).where(webhook_nonces.c.expires_ts < now_ts))
        values = {
            "tenant_id": tenant_id,
            "source": source,
            "nonce": nonce,
            "expires_ts": now_ts + ttl_s,
        }
        insert = pg_insert if self._uow.engine.dialect.name == "postgresql" else sqlite_insert
        result = conn.execute(
            insert(webhook_nonces).values(**values).on_conflict_do_nothing(),
            execution_options={"preserve_rowcount": True},
        )
        return int(result.rowcount) == 1


__all__ = ["SqlNonceStore", "WebhookInbox"]
