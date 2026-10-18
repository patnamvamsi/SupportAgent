"""Case rows (03 §2.2): persistence-first row-oriented access.

v0.0 has no ``domain/model/case.py`` (the Case domain model lands later);
the ``cases`` table is the source of truth and this repo is a thin, typed
row accessor (10-wp).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import cases
from supportagent.adapters.db.uow import SqlUnitOfWork


@dataclass(frozen=True)
class NewCase:
    """The column set for a new ``cases`` row (03 §2.2)."""

    id: str
    tenant_id: str
    fingerprint: str
    origin: str
    status: str
    opened_ts: int
    last_alert_ts: int
    trace_id: str
    severity: str | None = None
    service: str | None = None
    classification: str | None = None
    ticket_number: str | None = None
    external_table: str | None = None
    external_id: str | None = None
    group_id: str | None = None
    decision_json: str | None = None
    duplicate_count: int = 0


class CaseRepo:
    """Typed access to the ``cases`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def create(self, case: NewCase) -> None:
        """Insert a new case row; the partial unique index enforces one
        unresolved case per (tenant, fingerprint) (03 §2.2)."""
        values = {
            "id": case.id,
            "tenant_id": case.tenant_id,
            "fingerprint": case.fingerprint,
            "origin": case.origin,
            "status": case.status,
            "opened_ts": case.opened_ts,
            "last_alert_ts": case.last_alert_ts,
            "trace_id": case.trace_id,
            "severity": case.severity,
            "service": case.service,
            "classification": case.classification,
            "ticket_number": case.ticket_number,
            "external_table": case.external_table,
            "external_id": case.external_id,
            "group_id": case.group_id,
            "decision_json": case.decision_json,
            "duplicate_count": case.duplicate_count,
        }
        self._uow.connection.execute(sa.insert(cases).values(**values))

    def get(self, case_id: str) -> dict[str, Any] | None:
        """Load one case row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(cases).where(cases.c.id == case_id))
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None
