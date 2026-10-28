"""BL-040: the RCA page note is built from allowlisted fields and never overclaims."""

from __future__ import annotations

from supportagent.app.propose import rca_page_spec
from supportagent.domain.kinds import ActionKind


def _spec(**overrides: object) -> str:
    values: dict[str, object] = {
        "case_id": "case_1",
        "service": "payroll-api",
        "external_id": "sys-1",
        "integration": "pagerduty:payroll",
        "outcome": "SUPPORTED",
        "supported": "pg_lock_contention",
        "confirmed_by": ["postgres.locks"],
        "tested": ["pg_slow_query_regression", "pg_lock_contention"],
        "sop_pending": 0,
    }
    values.update(overrides)
    spec = rca_page_spec(**values)  # type: ignore[arg-type]
    assert spec.kind is ActionKind.ENRICH_PAGE
    assert spec.scope == "service:payroll-api:enrich_page"
    return str(spec.payload["text"])


def test_confirmed_cause_names_the_tools() -> None:
    assert _spec() == (
        "SupportAgent RCA for case case_1: SUPPORTED. Cause pg_lock_contention, confirmed by "
        "postgres.locks. Also checked: pg_slow_query_regression."
    )


def test_unentailed_cause_is_never_called_confirmed() -> None:
    text = _spec(confirmed_by=[])
    assert "confirmed by" not in text and "did not confirm it" in text


def test_no_cause_reports_what_was_checked() -> None:
    text = _spec(outcome="INCONCLUSIVE", supported=None, confirmed_by=[], sop_pending=0)
    assert text == (
        "SupportAgent RCA for case case_1: INCONCLUSIVE. Also checked: "
        "pg_slow_query_regression, pg_lock_contention."
    )
