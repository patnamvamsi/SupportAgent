"""Alert rows (03 §2.2): minimal ingest rows; the event store lands in 0002."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import alerts
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewAlert:
    """The column set for a new ``alerts`` row (03 §2.2)."""

    id: str
    tenant_id: str
    case_id: str
    adapter: str
    external_id: str
    payload_ref: str
    received_ts: int


class AlertRepo:
    """Typed access to the ``alerts`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, alert: NewAlert) -> None:
        """Insert an ingest row; ``payload_ref`` points at a ``blobs`` key (03 §2.2, §3)."""
        values = {
            "id": alert.id,
            "tenant_id": alert.tenant_id,
            "case_id": alert.case_id,
            "adapter": alert.adapter,
            "external_id": alert.external_id,
            "payload_ref": alert.payload_ref,
            "received_ts": alert.received_ts,
        }
        self._uow.connection.execute(sa.insert(alerts).values(**values))

    def list_for_case(self, case_id: str) -> list[dict[str, Any]]:
        """All ingest rows for a case, oldest first."""
        rows = self._uow.connection.execute(
            sa.select(alerts)
            .where(alerts.c.case_id == case_id)
            .order_by(alerts.c.received_ts, alerts.c.id)
        ).mappings()
        return [dict(row) for row in rows]
