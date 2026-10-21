"""BL-031: ``doctor`` saves its report and ``/ui/setup`` shows it without running checks."""

from __future__ import annotations

from typing import Any

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.doctor import CheckResult
from supportagent.app.doctor.base import CheckStatus, DoctorReport
from supportagent.app.queries import AdminQueries
from supportagent.bootstrap import save_doctor_report
from tests.unit.api_support import ADA, TENANT, catalog, login, make_client
from tests.unit.test_triage_service import _shadow_config


def _report(status: CheckStatus) -> DoctorReport:
    return DoctorReport(
        results=[
            CheckResult("config", "pass", "all files validate"),
            CheckResult("paging", status, "no paging provider configured for payments"),
        ]
    )


def test_doctor_report_saved_and_replaced(db: Any, clock: Any) -> None:
    config = _shadow_config()
    assert save_doctor_report(config, _report("warn"), clock=clock, engine=db)
    queries = AdminQueries(uow=SqlUnitOfWork(db), tenant_id=TENANT)
    first = queries.doctor_report()
    assert first is not None
    assert first["exit_code"] == _report("warn").exit_code
    checks: Any = first["checks"]
    assert [c["name"] for c in checks] == ["config", "paging"]
    clock.advance(60)
    save_doctor_report(config, _report("pass"), clock=clock, engine=db)
    latest = queries.doctor_report()
    assert latest is not None and latest["exit_code"] == 0
    assert latest["ran_ts"] > first["ran_ts"]  # type: ignore[operator]


def test_setup_page_shows_the_saved_report(
    db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    oidc = FakeOIDC()
    client = make_client(db, clock, ids, master_key, oidc, catalog("payments"))
    login(client, oidc, ADA)
    assert "No doctor report yet" in client.get("/ui/setup").text
    save_doctor_report(_shadow_config(), _report("warn"), clock=clock, engine=db)
    page = client.get("/ui/setup").text
    assert "no paging provider configured for payments" in page
    assert "runs nothing" in page
