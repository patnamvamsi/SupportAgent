"""StatsService (ARCH §8.4, CAP §4.7, 10-wp WP-014): mode-separated metrics.

``report(window, mode)`` never mixes regimes (ARCH §8.4):

- ``shadow``: rows from ``shadow_records`` — the counterfactual.
- ``assisted``: cases with **no** shadow record — the live pipeline; the first
  proposal is the earliest ``proposals.created_at_ts`` of the case.

Metrics, each with its denominator stated:

- **agreement** (shadow only in v0.0; it needs human labels): agreed / labelled
  rows, with the Wilson 95 % lower bound — the promotion gate reads the bound,
  never the raw rate (CAP §4.7). Also split into the ``P1-P2`` and ``P3-P4``
  bands of the *human* severity.
- **alert → first proposal** (seconds): nearest-rank p50 / p95 over cases that
  proposed; a safe default never proposes and is not a latency sample.
- **safe defaults**: count per reason, and safe defaults / decided cases.
- **abstain rate**: pre-classifier abstentions / cases that reached the
  pre-classifier (every decided case except ``unresolved-service``,
  ``no-paging-integration`` and ``spend-breaker``, which stop before it).
- **conflict rate**: ``classifier-conflict`` / reached cases where the
  pre-classifier produced a label (conflict is only possible then).

A rate with an empty denominator is ``None``, not 0.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Literal, Protocol, Self

import sqlalchemy as sa

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.promotion import PromotionGate, PromotionResult, evaluate
from supportagent.domain.stats import wilson_lower_bound
from supportagent.ports.clock import Clock

Mode = Literal["shadow", "assisted"]

#: Reasons decided before the pre-classifier runs (ARCH §4 branch order).
_BEFORE_PRECLASS = frozenset(
    {"unresolved-service", "no-paging-integration", "storm-member", "spend-breaker"}
)
_HIGH_BAND = frozenset({"P1", "P2"})


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


@dataclass(frozen=True, slots=True)
class AgreementStat:
    n: int
    agreed: int
    rate: float | None
    wilson_lb: float | None

    @classmethod
    def of(cls, flags: Sequence[bool]) -> AgreementStat:
        n, agreed = len(flags), sum(flags)
        if n == 0:
            return cls(n=0, agreed=0, rate=None, wilson_lb=None)
        return cls(n=n, agreed=agreed, rate=agreed / n, wilson_lb=wilson_lower_bound(agreed, n))


@dataclass(frozen=True, slots=True)
class LatencyStat:
    n: int
    p50: int | None
    p95: int | None


@dataclass(frozen=True, slots=True)
class FatigueStat:
    """Approval fatigue (CAP §4.1, 11-wp WP-115): measured, not assumed.

    ``approve_time_s`` is from proposal creation to the human approval
    (median and p90, nearest-rank). ``one_click_rate`` is the share of human
    approvals with no reason given, the rubber-stamp proxy.
    """

    approvals: int
    rejections: int
    approve_time_p50_s: int | None
    approve_time_p90_s: int | None
    one_click_rate: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StatsReport:
    mode: Mode
    window_days: float | None
    n_cases: int
    agreement: AgreementStat
    agreement_by_band: dict[str, AgreementStat]
    first_proposal_latency_s: LatencyStat
    safe_default_by_reason: dict[str, int] = field(default_factory=dict)
    safe_default_rate: float | None = None
    abstain_rate: float | None = None
    conflict_rate: float | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def percentile(values: Sequence[int], q: float) -> int | None:
    """Nearest-rank percentile: the smallest value with at least ``q`` of the data at or below."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


_UNITS = {"d": 86_400, "h": 3_600, "m": 60}


def parse_window(text: str) -> timedelta | None:
    """``30d`` / ``12h`` / ``90m`` → a window; ``all`` → ``None`` (no lower bound)."""
    value = text.strip().lower()
    if value == "all":
        return None
    unit = _UNITS.get(value[-1:])
    if unit is None or not value[:-1].isdigit() or int(value[:-1]) <= 0:
        raise DomainValidationError(  # noqa: TRY003
            f"window must look like 30d, 12h, 90m or 'all'; got {text!r}"
        )
    return timedelta(seconds=int(value[:-1]) * unit)


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


class StatsService:
    def __init__(self, *, uow: _UoW, clock: Clock, tenant_id: str) -> None:
        self._uow = uow
        self._clock = clock
        self._tenant = tenant_id

    def report(self, *, window: timedelta | None, mode: Mode) -> StatsReport:
        since = None if window is None else int((self._clock.now() - window).timestamp())
        rows = self._shadow_rows(since) if mode == "shadow" else self._assisted_rows(since)

        latencies = [int(r["first"]) - int(r["received"]) for r in rows if r["first"] is not None]
        labelled = [r for r in rows if r["agreement"] is not None]
        bands: dict[str, list[bool]] = {"P1-P2": [], "P3-P4": []}
        for r in labelled:
            band = "P1-P2" if r["human_severity"] in _HIGH_BAND else "P3-P4"
            bands[band].append(bool(r["agreement"]))

        decisions = [json.loads(r["decision_json"]) for r in rows if r["decision_json"]]
        safe = Counter(d["reason"] for d in decisions if d.get("outcome") == "safe_default")
        reached = [d for d in decisions if d.get("reason") not in _BEFORE_PRECLASS]
        with_label = [d for d in reached if d.get("preclass_label") is not None]
        conflicts = sum(1 for d in with_label if d.get("reason") == "classifier-conflict")

        return StatsReport(
            mode=mode,
            window_days=None if window is None else window.total_seconds() / 86_400,
            n_cases=len(rows),
            agreement=AgreementStat.of([bool(r["agreement"]) for r in labelled]),
            agreement_by_band={k: AgreementStat.of(v) for k, v in bands.items()},
            first_proposal_latency_s=LatencyStat(
                n=len(latencies), p50=percentile(latencies, 0.50), p95=percentile(latencies, 0.95)
            ),
            safe_default_by_reason=dict(sorted(safe.items())),
            safe_default_rate=_rate(sum(safe.values()), len(decisions)),
            abstain_rate=_rate(len(reached) - len(with_label), len(reached)),
            conflict_rate=_rate(conflicts, len(with_label)),
        )

    def promotion(self, gate: PromotionGate, *, window: timedelta | None = None) -> PromotionResult:
        """CAP §4.7: the shadow promotion gate on the agreement lower bounds."""
        report = self.report(window=window, mode="shadow")
        bands = {k: (v.agreed, v.n) for k, v in report.agreement_by_band.items()}
        return evaluate((report.agreement.agreed, report.agreement.n), bands, gate)

    def fatigue(self, *, window: timedelta | None = None) -> FatigueStat:
        """Approval fatigue over human decisions on proposals (excludes grant auto-approvals)."""
        since = None if window is None else int((self._clock.now() - window).timestamp())
        sql = (
            "SELECT a.status, a.reason, a.approved_at_ts - p.created_at_ts AS wait "
            "FROM approvals a JOIN proposals p ON p.id = a.proposal_id "
            "WHERE a.tenant_id = :tenant"
        )
        if since is not None:
            sql += " AND a.approved_at_ts >= :since"
        rows = self._fetch(sql, since)
        approved = [r for r in rows if r["status"] == "approved"]
        waits = [max(0, int(r["wait"])) for r in approved]
        one_click = sum(1 for r in approved if not (r["reason"] or "").strip())
        return FatigueStat(
            approvals=len(approved),
            rejections=sum(1 for r in rows if r["status"] == "rejected"),
            approve_time_p50_s=percentile(waits, 0.50),
            approve_time_p90_s=percentile(waits, 0.90),
            one_click_rate=_rate(one_click, len(approved)),
        )

    def _shadow_rows(self, since: int | None) -> list[sa.RowMapping]:
        sql = (
            "SELECT s.alert_received_ts AS received, s.first_proposal_ts AS first, "
            "s.human_severity AS human_severity, s.agreement AS agreement, "
            "c.decision_json AS decision_json FROM shadow_records s "
            "JOIN cases c ON c.id = s.case_id WHERE s.tenant_id = :tenant"
        )
        if since is not None:
            sql += " AND s.alert_received_ts >= :since"
        return self._fetch(sql, since)

    def _assisted_rows(self, since: int | None) -> list[sa.RowMapping]:
        sql = (
            "SELECT c.opened_ts AS received, "
            "(SELECT MIN(p.created_at_ts) FROM proposals p WHERE p.case_id = c.id) AS first, "
            "NULL AS human_severity, NULL AS agreement, c.decision_json AS decision_json "
            "FROM cases c WHERE c.tenant_id = :tenant AND c.decision_json IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM shadow_records s WHERE s.case_id = c.id)"
        )
        if since is not None:
            sql += " AND c.opened_ts >= :since"
        return self._fetch(sql, since)

    def _fetch(self, sql: str, since: int | None) -> list[sa.RowMapping]:
        params: dict[str, object] = {"tenant": self._tenant}
        if since is not None:
            params["since"] = since
        with self._uow:
            return list(self._uow.connection.execute(sa.text(sql), params).mappings())


__all__ = [
    "AgreementStat",
    "FatigueStat",
    "LatencyStat",
    "StatsReport",
    "StatsService",
    "parse_window",
    "percentile",
]
