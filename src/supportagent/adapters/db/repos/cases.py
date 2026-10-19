"""Case rows (03 §2.2): persistence-first row-oriented access.

v0.0 has no ``domain/model/case.py`` (the Case domain model lands later);
the ``cases`` table is the source of truth and this repo is a thin, typed
row accessor (10-wp).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import cases
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.model.upsert import UpsertResult

#: ARCH §4.1 — one unresolved case per (tenant, fingerprint) (INV-02).
#: The statement is identical on both engines ("Postgres twin: same
#: statement"); the conflict target is the partial unique index
#: ``ux_cases_unresolved`` (03 §2.2), so a resolved row never conflicts and
#: the next alert opens a new case. ``created`` derives from
#: ``duplicate_count = 0`` in RETURNING — SQLite's ``changes()`` cannot
#: distinguish insert from update (critique-v3). The DO UPDATE RHS is
#: table-qualified (``cases.duplicate_count``): on Postgres a bare column
#: there is ambiguous between the existing row and the ``excluded`` row,
#: while SQLite accepts the qualification identically.
_UPSERT = sa.text(
    """
    INSERT INTO cases
        (id, tenant_id, fingerprint, origin, status, trace_id,
         duplicate_count, opened_ts, last_alert_ts)
    VALUES
        (:id, :tenant_id, :fingerprint, :origin, 'open', :trace_id,
         0, :opened_ts, :last_alert_ts)
    ON CONFLICT (tenant_id, fingerprint) WHERE status <> 'resolved'
    DO UPDATE SET
        duplicate_count = cases.duplicate_count + 1,
        last_alert_ts = :last_alert_ts
    RETURNING id, duplicate_count = 0 AS created, duplicate_count
    """
)


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

    def upsert_by_fingerprint(  # noqa: PLR0913 — the 01 §7.3 spec signature
        self,
        *,
        case_id: str,
        tenant_id: str,
        fingerprint: str,
        origin: str,
        trace_id: str,
        now: datetime,
    ) -> UpsertResult:
        """One unresolved case per (tenant, fingerprint) (INV-02, ARCH §4.1).

        Inserts a skeleton case (status ``'open'``, ``duplicate_count 0``,
        ``opened_ts`` = ``last_alert_ts`` = ``now``) or, on conflict with
        the partial unique index ``ux_cases_unresolved`` (``status <>
        'resolved'``, 03 §2.2), bumps ``duplicate_count`` and
        ``last_alert_ts`` on the existing row — the index, not code
        ordering, is the enforcement. The 01 §7.3 sketch
        (``case_id, tenant_id, fingerprint, now``) is extended with
        ``origin`` and ``trace_id`` because both are NOT NULL (03 §2.2).
        ``created`` comes from RETURNING (see ``_UPSERT``); SQLite returns
        it as an int, Postgres as a bool — normalized here.
        """
        ts = int(now.timestamp())
        row = (
            self._uow.connection.execute(
                _UPSERT,
                {
                    "id": case_id,
                    "tenant_id": tenant_id,
                    "fingerprint": fingerprint,
                    "origin": origin,
                    "trace_id": trace_id,
                    "opened_ts": ts,
                    "last_alert_ts": ts,
                },
            )
            .mappings()
            .one()
        )
        return UpsertResult(
            case_id=str(row["id"]),
            created=bool(row["created"]),
            duplicate_count=int(row["duplicate_count"]),
        )
