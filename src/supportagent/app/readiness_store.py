"""The system's last readiness report (ADR-0015 §5, WP-O6b): saved, loaded, as a policy.

``onboard readiness`` saves it; the triage and RCA wiring read it as a
:class:`~supportagent.domain.readiness.ReadinessPolicy`, and
``/ui/onboarding/readiness`` renders it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.readiness import ReadinessPolicy
from supportagent.ports.clock import Clock


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class ReadinessStore:
    def __init__(self, *, uow: _UoW, clock: Clock, tenant_id: str) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id

    def save(self, report: Mapping[str, Any]) -> None:
        values = {
            "t": self._tenant,
            "r": json.dumps(report, sort_keys=True),
            "now": int(self._clock.now().timestamp()),
        }
        with self._uow:
            conn = self._uow.connection
            updated = conn.execute(
                sa.text(
                    "UPDATE onboarding_readiness SET report_json = :r, updated_ts = :now "
                    "WHERE tenant_id = :t"
                ),
                values,
            ).rowcount
            if not updated:
                conn.execute(
                    sa.text(
                        "INSERT INTO onboarding_readiness (tenant_id, report_json, updated_ts) "
                        "VALUES (:t, :r, :now)"
                    ),
                    values,
                )
            self._uow.commit()

    def load(self) -> dict[str, Any] | None:
        with self._uow:
            row = self._uow.connection.execute(
                sa.text("SELECT report_json FROM onboarding_readiness WHERE tenant_id = :t"),
                {"t": self._tenant},
            ).first()
        if row is None:
            return None
        report = json.loads(str(row[0]))
        return report if isinstance(report, dict) else None

    def policy(self) -> ReadinessPolicy | None:
        """``None`` until the system has a report: routing is then unchanged."""
        report = self.load()
        if report is None:
            return None
        classes = report.get("classes")
        return (
            ReadinessPolicy.of(c for c in classes if isinstance(c, dict))
            if isinstance(classes, list)
            else None
        )


__all__ = ["ReadinessStore"]
