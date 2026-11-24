"""Onboarding's per-tenant documents: the last readiness report (ADR-0015 §5, WP-O6b), as a
policy, and the last ``onboard run``'s status (ADR-0017, WP-A4).

``onboard readiness`` saves it; the triage and RCA wiring read it as a
:class:`~supportagent.domain.readiness.ReadinessPolicy`, and
``/ui/onboarding/readiness`` renders it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.readiness import ReadinessPolicy
from supportagent.ports.clock import Clock


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def commit(self) -> None: ...

    def reading(self) -> AbstractContextManager[sa.Connection]: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class TenantDocument:
    """One JSON document per tenant in a ``(tenant_id, <column>, updated_ts)`` table."""

    #: Set by each subclass: constants, never input.
    table: str = ""
    column: str = ""

    def __init__(self, *, uow: _UoW, clock: Clock, tenant_id: str) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id

    def _table(self) -> sa.TableClause:
        return sa.table(
            self.table, sa.column("tenant_id"), sa.column(self.column), sa.column("updated_ts")
        )

    def save(self, document: Mapping[str, Any]) -> None:
        t = self._table()
        body = json.dumps(document, sort_keys=True)
        now = int(self._clock.now().timestamp())
        with self._uow:
            conn = self._uow.connection
            updated = conn.execute(
                sa.update(t)
                .where(t.c.tenant_id == self._tenant)
                .values({self.column: body, "updated_ts": now})
            ).rowcount
            if not updated:
                conn.execute(
                    sa.insert(t).values(
                        {"tenant_id": self._tenant, self.column: body, "updated_ts": now}
                    )
                )
            self._uow.commit()

    def load(self) -> dict[str, Any] | None:
        t = self._table()
        with self._uow.reading() as conn:
            row = conn.execute(
                sa.select(t.c[self.column]).where(t.c.tenant_id == self._tenant)
            ).first()
        if row is None:
            return None
        document = json.loads(str(row[0]))
        return document if isinstance(document, dict) else None


class ReadinessStore(TenantDocument):
    """The system's last readiness report (WP-O6b)."""

    table, column = "onboarding_readiness", "report_json"

    def policy(self) -> ReadinessPolicy | None:
        """``None`` until the system has a report: routing is then unchanged."""
        report = self.load()
        if report is None:
            return None
        classes = report.get("classes")
        if not isinstance(classes, list):
            return None
        return ReadinessPolicy.of(c for c in classes if isinstance(c, dict))


class OnboardingStatusStore(TenantDocument):
    """The last ``onboard run``: its steps and what it waits on a person for (ADR-0017)."""

    table, column = "onboarding_status", "status_json"


__all__ = ["OnboardingStatusStore", "ReadinessStore", "TenantDocument"]
