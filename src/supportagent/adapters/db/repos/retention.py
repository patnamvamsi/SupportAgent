"""The retention sweep's delete path (03 §2.4, 02 §7; BL-028).

``events`` is append-only (``trg_events_append_only`` aborts any UPDATE),
and this is the **only** code that deletes from it. Audit tables
(proposals, approvals, policy_grants, outbox, feedback, shadow_records,
users, role_assignments) are never swept.

- ``events`` older than ``events_days`` (by ``ts_ms``);
- ``alerts`` older than ``alerts_days`` (by ``received_ts``) and the raw
  payload blobs they point at, plus processed ``webhook_inbox`` rows;
- sent ``comms`` and ``comms_drafts`` older than ``comms_days``.

Each call runs in the caller's unit of work and returns row counts only.
"""

from __future__ import annotations

import sqlalchemy as sa

from supportagent.adapters.db.tables import (
    alerts,
    blobs,
    comms,
    comms_drafts,
    events,
    webhook_inbox,
)
from supportagent.adapters.db.uow import SqlUnitOfWork


class SqlRetention:
    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def purge(
        self,
        tenant_id: str,
        *,
        events_before_ms: int,
        alerts_before_ts: int,
        comms_before_ts: int,
    ) -> dict[str, int]:
        conn = self._uow.connection
        old_alerts = sa.select(alerts.c.payload_ref).where(
            alerts.c.tenant_id == tenant_id, alerts.c.received_ts < alerts_before_ts
        )
        counts = {
            "events": conn.execute(
                sa.delete(events).where(
                    events.c.tenant_id == tenant_id, events.c.ts_ms < events_before_ms
                )
            ).rowcount,
            "blobs": conn.execute(
                sa.delete(blobs).where(
                    blobs.c.tenant_id == tenant_id, blobs.c.key.in_(old_alerts.scalar_subquery())
                )
            ).rowcount,
            "alerts": conn.execute(
                sa.delete(alerts).where(
                    alerts.c.tenant_id == tenant_id, alerts.c.received_ts < alerts_before_ts
                )
            ).rowcount,
            "webhook_inbox": conn.execute(
                sa.delete(webhook_inbox).where(
                    webhook_inbox.c.tenant_id == tenant_id,
                    webhook_inbox.c.processed_ts.is_not(None),
                    webhook_inbox.c.processed_ts < alerts_before_ts,
                )
            ).rowcount,
            "comms": conn.execute(
                sa.delete(comms).where(
                    comms.c.tenant_id == tenant_id,
                    comms.c.sent_at_ts.is_not(None),
                    comms.c.sent_at_ts < comms_before_ts,
                )
            ).rowcount,
            "comms_drafts": conn.execute(
                sa.delete(comms_drafts).where(
                    comms_drafts.c.tenant_id == tenant_id,
                    comms_drafts.c.created_ts < comms_before_ts,
                )
            ).rowcount,
        }
        return {k: int(v or 0) for k, v in counts.items()}


__all__ = ["SqlRetention"]
