"""``shadow_records`` repository (03 §2.6, WP-014)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.tables import shadow_records
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.domain.model.shadow import ShadowRecord
from supportagent.domain.severity import Severity


def _ts(value: datetime | None) -> int | None:
    return None if value is None else int(value.timestamp())


def _dt(value: int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value), UTC)


class ShadowRepo:
    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def insert(self, record: ShadowRecord) -> None:
        self._uow.connection.execute(
            sa.insert(shadow_records).values(
                id=record.id,
                tenant_id=record.tenant_id,
                case_id=record.case_id,
                alert_received_ts=_ts(record.alert_received_at),
                first_proposal_ts=_ts(record.first_proposal_at),
                proposed_json=json.dumps(dict(record.proposed), sort_keys=True),
                human_classification=record.human_classification,
                human_severity=record.human_severity.value if record.human_severity else None,
                human_first_action_ts=_ts(record.human_first_action_at),
                agreement=None if record.agreement is None else int(record.agreement),
            )
        )

    def by_case(self, tenant_id: str, case_id: str) -> ShadowRecord | None:
        row = (
            self._uow.connection.execute(
                sa.select(shadow_records).where(
                    shadow_records.c.tenant_id == tenant_id, shadow_records.c.case_id == case_id
                )
            )
            .mappings()
            .first()
        )
        return None if row is None else _record(row)

    def set_human(
        self,
        record_id: str,
        *,
        classification: str | None,
        severity: Severity | None,
        first_action_at: datetime | None,
        agreement: bool | None,
    ) -> None:
        self._uow.connection.execute(
            sa.update(shadow_records)
            .where(shadow_records.c.id == record_id)
            .values(
                human_classification=classification,
                human_severity=severity.value if severity else None,
                human_first_action_ts=_ts(first_action_at),
                agreement=None if agreement is None else int(agreement),
            )
        )


def _record(row: Any) -> ShadowRecord:  # noqa: ANN401 — a SQLAlchemy RowMapping
    agreement = row["agreement"]
    return ShadowRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        case_id=row["case_id"],
        alert_received_at=datetime.fromtimestamp(int(row["alert_received_ts"]), UTC),
        first_proposal_at=_dt(row["first_proposal_ts"]),
        proposed=json.loads(row["proposed_json"]),
        human_classification=row["human_classification"],
        human_severity=Severity(row["human_severity"]) if row["human_severity"] else None,
        human_first_action_at=_dt(row["human_first_action_ts"]),
        agreement=None if agreement is None else bool(agreement),
    )
