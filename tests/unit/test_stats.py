"""WP-014: StatsService — mode-separated counterfactual metrics (ARCH §8.4).

Canonical test IDs from 10-wp §WP-014: ``test_stats_agreement_wilson_lb``,
``test_stats_mode_separated``, ``test_stats_first_proposal_latency_percentiles``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.stats import StatsService, parse_window, percentile
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.stats import wilson_lower_bound

T0 = 1_790_000_000  # alert time base (epoch seconds)


def _decision(reason: str = "routed", preclass: str | None = "payments/api") -> str:
    outcome = "routed" if reason == "routed" else "safe_default"
    return json.dumps(
        {
            "outcome": outcome,
            "reason": reason,
            "classification": "payments/api",
            "severity": "P3",
            "preclass_label": preclass,
            "calibrated_score": 0.9,
            "route_id": "r" if outcome == "routed" else None,
            "llm_suggestion": None,
        }
    )


def _case(conn: Any, case_id: str, *, opened_ts: int, decision: str | None = None) -> None:
    conn.execute(
        text(
            "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
            "duplicate_count, opened_ts, last_alert_ts, decision_json) VALUES (:id, 'acme', "
            ":fp, 'alert', 'triaged', :tr, 0, :ts, :ts, :dj)"
        ),
        {
            "id": case_id,
            "fp": f"fp_{case_id}",
            "tr": f"tr_{case_id}",
            "ts": opened_ts,
            "dj": decision or _decision(),
        },
    )


def _shadow(  # noqa: PLR0913 — one knob per column
    db: Any,
    rid: str,
    *,
    received: int = T0,
    first: int | None = None,
    reason: str = "routed",
    preclass: str | None = "payments/api",
    agreement: int | None = None,
    human_severity: str | None = "P3",
) -> None:
    decision = _decision(reason, preclass)
    with db.begin() as conn:
        _case(conn, f"case_{rid}", opened_ts=received, decision=decision)
        conn.execute(
            text(
                "INSERT INTO shadow_records (id, tenant_id, case_id, alert_received_ts, "
                "first_proposal_ts, proposed_json, human_classification, human_severity, "
                "agreement) VALUES (:id, 'acme', :case, :rcv, :first, :pj, 'payments/api', "
                ":hs, :agr)"
            ),
            {
                "id": rid,
                "case": f"case_{rid}",
                "rcv": received,
                "first": first,
                "pj": json.dumps({"decision": json.loads(decision), "specs": []}),
                "hs": human_severity,
                "agr": agreement,
            },
        )


@pytest.fixture
def stats(db: Any, clock: Any) -> StatsService:
    return StatsService(uow=SqlUnitOfWork(db), clock=clock, tenant_id="acme")


def test_percentile_nearest_rank() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(values, 0.50) == 50
    assert percentile(values, 0.95) == 100
    assert percentile([7], 0.95) == 7
    assert percentile([], 0.5) is None


def test_stats_agreement_wilson_lb(stats: StatsService, db: Any) -> None:
    for i in range(30):
        _shadow(db, f"a{i}", agreement=1 if i < 24 else 0)  # 24/30 agree
    _shadow(db, "unlabelled", agreement=None)  # no human label: not in the denominator
    for i in range(5):
        _shadow(db, f"p1_{i}", agreement=1, human_severity="P1")
    report = stats.report(window=None, mode="shadow")
    agree = report.agreement
    assert (agree.n, agree.agreed) == (35, 29)
    assert agree.rate == pytest.approx(29 / 35)
    assert agree.wilson_lb == pytest.approx(wilson_lower_bound(29, 35))
    assert agree.wilson_lb is not None and agree.rate is not None
    assert agree.wilson_lb < agree.rate, "the gate reads the lower bound, never the raw rate"
    bands = report.agreement_by_band
    assert (bands["P1-P2"].n, bands["P1-P2"].agreed) == (5, 5)
    assert (bands["P3-P4"].n, bands["P3-P4"].agreed) == (30, 24)
    assert report.n_cases == 36


def test_stats_first_proposal_latency_percentiles(stats: StatsService, db: Any) -> None:
    for i, latency in enumerate(range(10, 101, 10)):
        _shadow(db, f"l{i}", received=T0 + i, first=T0 + i + latency)
    _shadow(db, "safe", reason="low-calibrated-score", first=None)  # never proposed
    report = stats.report(window=None, mode="shadow")
    lat = report.first_proposal_latency_s
    assert (lat.n, lat.p50, lat.p95) == (10, 50, 100)


def test_stats_safe_default_conflict_and_abstain_rates(stats: StatsService, db: Any) -> None:
    _shadow(db, "r1")
    _shadow(db, "r2", preclass=None)  # routed on an abstain
    _shadow(db, "c1", reason="classifier-conflict")
    _shadow(db, "u1", reason="unresolved-service", preclass=None)  # never reached preclass
    _shadow(db, "l1", reason="llm-failure", preclass=None)
    report = stats.report(window=None, mode="shadow")
    assert report.safe_default_by_reason == {
        "classifier-conflict": 1,
        "llm-failure": 1,
        "unresolved-service": 1,
    }
    assert report.safe_default_rate == pytest.approx(3 / 5)
    # Pre-classifier denominators exclude cases that never reached it (unresolved-service).
    assert report.abstain_rate == pytest.approx(2 / 4)  # r2, l1 of r1, r2, c1, l1
    assert report.conflict_rate == pytest.approx(1 / 2)  # c1 of the 2 labelled (r1, c1)


def test_stats_mode_separated(stats: StatsService, db: Any) -> None:
    # ARCH §8.4: shadow and assisted are never mixed in one report.
    _shadow(db, "s1", first=T0 + 30, agreement=1)
    with db.begin() as conn:
        _case(conn, "live1", opened_ts=T0)
        conn.execute(
            text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, payload_json, "
                "payload_hash, trace_id, status, created_by, created_at_ts) VALUES ('p1', "
                "'acme', 'live1', 'work_note', 's', '{}', 'h', 't', 'pending_approval', "
                "'agent', :ts)"
            ),
            {"ts": T0 + 90},
        )
    shadow = stats.report(window=None, mode="shadow")
    assisted = stats.report(window=None, mode="assisted")
    assert (shadow.mode, shadow.n_cases) == ("shadow", 1)
    assert (assisted.mode, assisted.n_cases) == ("assisted", 1)
    assert shadow.first_proposal_latency_s.p50 == 30
    assert assisted.first_proposal_latency_s.p50 == 90
    assert shadow.agreement.n == 1
    assert assisted.agreement.n == 0, "agreement needs human labels: shadow only in v0.0"
    assert shadow.as_dict()["mode"] == "shadow"


def test_stats_window_filters_by_alert_time(stats: StatsService, db: Any, clock: Any) -> None:
    now = int(clock.now().timestamp())
    _shadow(db, "old", received=now - int(timedelta(days=40).total_seconds()))
    _shadow(db, "new", received=now - int(timedelta(days=2).total_seconds()))
    assert stats.report(window=timedelta(days=30), mode="shadow").n_cases == 1
    assert stats.report(window=None, mode="shadow").n_cases == 2


def test_parse_window() -> None:
    assert parse_window("30d") == timedelta(days=30)
    assert parse_window("12h") == timedelta(hours=12)
    assert parse_window("all") is None
    for bad in ("", "30", "d", "-1d", "0d", "3w"):
        with pytest.raises(DomainValidationError):
            parse_window(bad)
