"""SpendBreaker (ARCH §11, §4.3; 11-wp WP-111): the per-tenant daily LLM budget.

Today's spend is the sum of ``tokens_in + tokens_out`` over the tenant's
``llm`` events since 00:00 UTC. Once it reaches ``llm.daily_budget_tokens``
the system degrades to **safe-default-only**: triage decides
``spend-breaker`` without calling the LLM, and records a ``spend_breaker``
event. It's a component, not a flag. The config can move the threshold
(``daily_budget_tokens > 0``) but can't turn it off
(``test_spend_breaker_cannot_be_disabled_by_config``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self

import sqlalchemy as sa

from supportagent.domain.errors import ConfigError


class _UoW(Protocol):
    @property
    def connection(self) -> sa.Connection: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class SpendBreaker:
    def __init__(self, *, uow: _UoW, daily_budget_tokens: int) -> None:
        if daily_budget_tokens <= 0:
            raise ConfigError(  # noqa: TRY003
                "llm.daily_budget_tokens must be positive: the spend breaker can't be disabled"
            )
        self._uow = uow
        self.budget = daily_budget_tokens

    def used_today(self, tenant_id: str, now: datetime) -> int:
        start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._uow:
            used = self._uow.connection.execute(
                sa.text(
                    "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) FROM events "
                    "WHERE tenant_id = :t AND kind = 'llm' AND ts_ms >= :since"
                ),
                {"t": tenant_id, "since": int(start.timestamp() * 1000)},
            ).scalar_one()
        return int(used)

    def allow(self, tenant_id: str, now: datetime) -> bool:
        return self.used_today(tenant_id, now) < self.budget


__all__ = ["SpendBreaker"]
