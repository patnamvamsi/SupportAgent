"""The last ``doctor`` report per tenant (BL-031): written by the CLI, read by ``/ui/setup``."""

from __future__ import annotations

import json
from collections.abc import Mapping

from sqlalchemy import Engine

from supportagent.adapters.db.tables import doctor_reports
from supportagent.adapters.db.upsert import upsert_stmt


class SqlDoctorReports:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, tenant_id: str, report: Mapping[str, object], *, ran_ts: int) -> None:
        values = {
            "tenant_id": tenant_id,
            "report_json": json.dumps(report, sort_keys=True),
            "ran_ts": ran_ts,
        }
        with self._engine.begin() as conn:
            conn.execute(
                upsert_stmt(
                    self._engine,
                    doctor_reports,
                    values,
                    ["tenant_id"],
                    {"report_json": values["report_json"], "ran_ts": ran_ts},
                )
            )


__all__ = ["SqlDoctorReports"]
