"""WP-015: ``doctor`` checks (02 §4.1), one test per check and status.

Canonical test IDs from 10-wp §WP-015: ``test_doctor_<check>_<status>``,
``test_doctor_exit_codes``, ``test_doctor_paging_missing_blocks_triage``
(``test_doctor_never_writes_to_servicenow`` is in the WP-015 DoD suite).
ServiceNow is FakeServiceNowHTTP behind the real read-only Table API probe.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.adapters.servicenow.auth import BasicAuth
from supportagent.adapters.servicenow.http import ServiceNowHTTP, build_client
from supportagent.adapters.servicenow.probe import ServiceNowTableProbe
from supportagent.app.doctor import Bundle, CheckResult, Doctor, DoctorContext
from supportagent.app.doctor.checks.endpoints import AutomationActorsCheck, LLMCheck, OIDCCheck
from supportagent.app.doctor.checks.local import ConfigCheck, DbCheck, PagingCheck
from supportagent.app.doctor.checks.servicenow import (
    AuditRetentionCheck,
    AuthCheck,
    IntegrationRoleCheck,
    JournalVisibilityCheck,
    RateLimitCheck,
)
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError, LLMUnavailable
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.triage import SafeDefaulted
from tests.unit.doctor_support import healthy_instance
from tests.unit.test_triage_service import _evt, _shadow_config, _wire

_MODEL = "claude-sonnet-4-5-20250929"


class _Schema:
    def __init__(self, current: str | None = "0006", fk: bool | None = True) -> None:
        self.current, self.fk = current, fk

    def current_revision(self) -> str | None:
        return self.current

    def head_revision(self) -> str:
        return "0006"

    def foreign_keys_enforced(self) -> bool | None:
        return self.fk


class _LLM:
    def __init__(self, resolve: Callable[[str], str] = lambda m: m) -> None:
        self._resolve = resolve

    def probe(self, model_id: str) -> str:
        return self._resolve(model_id)


class _Actors:
    def __init__(self, confirmed: int = 0) -> None:
        self.rows: dict[str, str] = {}
        self.confirmed = confirmed

    def add_candidates(self, tenant_id: str, actors: Any) -> int:
        new = [(u, k) for u, k in actors if u not in self.rows]
        self.rows.update(new)
        return len(new)

    def summary(self, tenant_id: str) -> tuple[int, int]:
        return len(self.rows), self.confirmed


class _DeadIdP:
    def discover(self) -> str:
        raise LLMUnavailable("connection refused")  # noqa: TRY003 — any ExternalServiceError


def _service(name: str, paging: str | None = "pagerduty:x") -> ServiceEntry:
    return ServiceEntry(name=name, cmdb_ci=[f"CI{name.upper()}"], paging_integration=paging)


def _bundle(catalog: Catalog | None = None) -> Bundle:
    from supportagent.adapters.files.taxonomy_loader import (  # noqa: PLC0415
        load_classification,
        load_hypotheses,
    )

    return Bundle(
        catalog=catalog or Catalog(version="1", services=[_service("payments")]),
        taxonomy=load_classification(_repo() / "taxonomy" / "classification.yaml"),
        hypotheses=load_hypotheses(_repo() / "taxonomy" / "hypotheses.yaml"),
    )


def _repo() -> Any:
    from pathlib import Path  # noqa: PLC0415

    return Path(__file__).resolve().parents[2]


def _calibration() -> CalibrationTable:
    return CalibrationTable(grade="smoke", snapshot_id="s", model_id=_MODEL, labels={})


def _ctx(  # noqa: PLR0913 — one knob per port
    sn: FakeServiceNowHTTP | None = None,
    *,
    clock: Any,
    config: Any = None,
    bundle: Callable[[], Bundle] = _bundle,
    calibration: Callable[[], CalibrationTable] = _calibration,
    schema: Any = None,
    llm: Any = None,
    identity: Any = None,
    automation: Any = None,
    discover: bool = False,
) -> DoctorContext:
    sn = sn if sn is not None else healthy_instance(FakeServiceNowHTTP())
    http = ServiceNowHTTP(
        http=build_client("https://acme.service-now.com", transport=SyncASGITransport(sn.app)),
        auth=BasicAuth(user="svc", password="pw-fake"),  # noqa: S106 — fake
        max_tries=1,
    )
    return DoctorContext(
        config=config if config is not None else _shadow_config(),
        load_bundle=bundle,
        load_calibration=calibration,
        schema=schema or _Schema(),
        servicenow=ServiceNowTableProbe(http=http),
        llm=llm or _LLM(),
        identity=identity or FakeOIDC(),
        automation=automation if automation is not None else _Actors(),
        clock=clock,
        discover_automation=discover,
    )


def _sn() -> FakeServiceNowHTTP:
    return healthy_instance(FakeServiceNowHTTP())


# --- config ------------------------------------------------------------------


def test_doctor_config_pass(clock: Any) -> None:
    res = ConfigCheck().run(_ctx(clock=clock))
    assert res.status == "pass", res.detail


def test_doctor_config_fail(clock: Any) -> None:
    def bad_calibration() -> CalibrationTable:
        raise ConfigError("calibration grade smoke below holdout")  # noqa: TRY003

    base = _shadow_config()
    unknown = base.routing[0].model_copy(
        update={
            "match": base.routing[0].match.model_copy(update={"classification": "nope/missing"})
        }
    )
    cfg = base.model_copy(update={"routing": [unknown]})
    res = ConfigCheck().run(_ctx(clock=clock, config=cfg, calibration=bad_calibration))
    assert res.status == "fail"
    assert "2 issue(s)" in res.detail and "nope/missing" in res.detail and "holdout" in res.detail


# --- db ----------------------------------------------------------------------


def test_doctor_db_pass(clock: Any) -> None:
    assert DbCheck().run(_ctx(clock=clock)).status == "pass"


@pytest.mark.parametrize(
    ("schema", "needle"),
    [(_Schema(current="0004"), "supportagent migrate"), (_Schema(fk=False), "foreign_keys")],
)
def test_doctor_db_fail(clock: Any, schema: _Schema, needle: str) -> None:
    res = DbCheck().run(_ctx(clock=clock, schema=schema))
    assert res.status == "fail" and needle in res.detail


# --- servicenow.auth ---------------------------------------------------------


def test_doctor_servicenow_auth_pass(clock: Any) -> None:
    assert AuthCheck().run(_ctx(clock=clock)).status == "pass"


def test_doctor_servicenow_auth_fail(clock: Any) -> None:
    sn = _sn()
    sn.queue_status(401, body={"error": {"message": "User Not Authenticated"}})
    res = Doctor([AuthCheck()]).run(_ctx(sn, clock=clock)).results[0]
    assert res.status == "fail" and "401" in res.detail


# --- servicenow.journal_visibility -------------------------------------------


def test_doctor_servicenow_journal_visibility_pass(clock: Any) -> None:
    assert JournalVisibilityCheck().run(_ctx(clock=clock)).status == "pass"


def test_doctor_servicenow_journal_visibility_warn(clock: Any) -> None:
    sn = _sn()
    sn.forbid("sys_security_acl")
    res = JournalVisibilityCheck().run(_ctx(sn, clock=clock))
    assert res.status == "warn" and "verify manually" in res.detail


@pytest.mark.parametrize("variant", ["external-role", "no-role"])
def test_doctor_servicenow_journal_visibility_fail(clock: Any, variant: str) -> None:
    sn = _sn()
    sn.seed_row(
        "sys_security_acl",
        {
            "sys_id": "acl-open",
            "name": "incident.work_notes",
            "operation": "read",
            "active": "true",
        },
    )
    if variant == "external-role":
        sn.seed_row(
            "sys_security_acl_role",
            {"sys_id": "ar-x", "sys_security_acl": "acl-open", "sys_user_role": "r-ext"},
        )
    res = JournalVisibilityCheck().run(_ctx(sn, clock=clock))
    assert res.status == "fail"
    assert "incident.work_notes" in res.detail


# --- servicenow.integration_role ---------------------------------------------


def test_doctor_servicenow_integration_role_pass(clock: Any) -> None:
    res = IntegrationRoleCheck().run(_ctx(clock=clock))
    assert res.status == "pass", res.detail


def test_doctor_servicenow_integration_role_warn(clock: Any) -> None:
    sn = _sn()
    sn.seed_row("sys_user_role", {"sys_id": "r-admin", "name": "admin"})
    sn.seed_row(
        "sys_user_has_role",
        {"sys_id": "uhr-2", "user": "u-svc", "role": "r-admin", "state": "active"},
    )
    sn.seed_row(
        "sys_security_acl",
        {"sys_id": "acl-chg", "name": "change_request", "operation": "write", "active": "true"},
    )
    sn.seed_row(
        "sys_security_acl_role",
        {"sys_id": "ar-3", "sys_security_acl": "acl-chg", "sys_user_role": "r-itil"},
    )
    res = IntegrationRoleCheck().run(_ctx(sn, clock=clock))
    assert res.status == "warn"
    assert res.data["broad_roles"] == ["admin"]
    assert res.data["extra_write_acls"] == ["change_request"]


def test_doctor_servicenow_integration_role_fail(clock: Any) -> None:
    sn = FakeServiceNowHTTP()  # no sys_user for the integration user
    res = IntegrationRoleCheck().run(_ctx(sn, clock=clock))
    assert res.status == "fail" and "not found" in res.detail


# --- servicenow.rate_limit ---------------------------------------------------


def test_doctor_servicenow_rate_limit_pass(clock: Any) -> None:
    res = RateLimitCheck().run(_ctx(clock=clock))
    assert res.status == "pass"
    assert res.data["recommended_poll_interval_s"] == 5  # 1 table x 3 calls x 3600 / 2500


def test_doctor_servicenow_rate_limit_pass_without_headers(clock: Any) -> None:
    sn = _sn()
    sn.set_response_headers({})
    res = RateLimitCheck().run(_ctx(sn, clock=clock))
    assert res.status == "pass" and "no rate-limit rule" in res.detail


@pytest.mark.parametrize(
    ("headers", "needle"),
    [
        ({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "100"}, "requests left"),
        ({"X-RateLimit-Limit": "200", "X-RateLimit-Remaining": "200"}, "raise it to 108"),
    ],
)
def test_doctor_servicenow_rate_limit_warn(
    clock: Any, headers: dict[str, str], needle: str
) -> None:
    sn = _sn()
    sn.set_response_headers(headers)
    res = RateLimitCheck().run(_ctx(sn, clock=clock))
    assert res.status == "warn" and needle in res.detail


# --- servicenow.audit_retention ----------------------------------------------


def test_doctor_servicenow_audit_retention_pass(clock: Any) -> None:
    res = AuditRetentionCheck().run(_ctx(clock=clock))
    assert res.status == "pass" and res.data["retention_days"] == 90.0


def test_doctor_servicenow_audit_retention_warn(clock: Any) -> None:
    sn = _sn()
    sn.seed_row(
        "sys_auto_flush",
        {"sys_id": "af-1", "tablename": "sys_audit", "age": str(3 * 86_400), "active": "true"},
    )
    res = AuditRetentionCheck().run(_ctx(sn, clock=clock))
    assert res.status == "warn" and "3.0 days" in res.detail


def test_doctor_servicenow_audit_retention_warn_when_unreadable(clock: Any) -> None:
    sn = _sn()
    sn.forbid("sys_auto_flush")
    assert AuditRetentionCheck().run(_ctx(sn, clock=clock)).status == "warn"


# --- automation_actors -------------------------------------------------------


def test_doctor_automation_actors_pass(clock: Any) -> None:
    sn = _sn()
    for user, flags in (
        ("flow.designer", {"internal_integration_user": "true"}),
        ("api.monitor", {"web_service_access_only": "true"}),
        ("alice", {}),  # a person: never a candidate
    ):
        sn.seed_audit(
            "incident",
            "inc-1",
            user=user,
            ts=clock.now(),
            fieldname="assigned_to",
            newvalue="u2",
            checkpoint=2,
        )
        sn.seed_row("sys_user", {"sys_id": f"u-{user}", "user_name": user, **flags})
    sn.seed_audit(
        "incident",
        "inc-1",
        user="svc_supportagent",
        ts=clock.now(),
        fieldname="assigned_to",
        checkpoint=3,
    )
    actors = _Actors()
    res = AutomationActorsCheck().run(_ctx(sn, clock=clock, automation=actors, discover=True))
    assert res.status == "pass"
    assert actors.rows == {"api.monitor": "web_service", "flow.designer": "integration"}
    assert res.data["added"] == 2
    # Re-running finds the same actors and adds nothing.
    again = AutomationActorsCheck().run(_ctx(sn, clock=clock, automation=actors, discover=True))
    assert again.data["added"] == 0


def test_doctor_automation_actors_fail(clock: Any) -> None:
    actors = _Actors()
    actors.add_candidates("acme", [("flow.designer", "integration")])
    assisted = _shadow_config().model_copy(update={"mode": "assisted"})
    res = AutomationActorsCheck().run(_ctx(clock=clock, config=assisted, automation=actors))
    assert res.status == "fail" and "must confirm" in res.detail


# --- paging ------------------------------------------------------------------


def _with_pagerduty() -> Any:
    data = _shadow_config().model_dump(mode="json")
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "d-" + secret + "-" + "x" * 32
    data["servicenow"]["auth"] = {"kind": "basic", "user": "svc", "password": "pw-fake"}
    data["paging"] = {"pagerduty": {"token": "pd-fake", "from_email": "sa@acme.example"}}
    return AppConfig.model_validate(data)


def test_doctor_paging_pass(clock: Any) -> None:
    assert PagingCheck().run(_ctx(clock=clock, config=_with_pagerduty())).status == "pass"


def test_doctor_paging_warns_on_null_object(clock: Any) -> None:
    """WP-112: a paging_integration whose provider isn't configured gets the Null Object."""
    res = PagingCheck().run(_ctx(clock=clock))
    assert res.status == "warn"
    assert res.data["null_paging"] == ["payments"]
    assert "enrich_page is a no-op" in res.detail


def test_doctor_paging_fail(clock: Any) -> None:
    catalog = Catalog(version="1", services=[_service("payments"), _service("search", None)])
    res = PagingCheck().run(_ctx(clock=clock, bundle=lambda: _bundle(catalog)))
    assert res.status == "fail" and res.data["unpaged"] == ["search"]


def test_doctor_paging_missing_blocks_triage(
    uow: Any, clock: Any, ids: Any, aead: Any, db: Any
) -> None:
    """ARCH §14: doctor flags the service, and triage refuses it (ADR-0006)."""
    unpaged = Catalog(
        version="1",
        services=[
            ServiceEntry(name="payments", cmdb_ci=["CIPAYMENTS"], resources=["payments-api"])
        ],
    )
    res = PagingCheck().run(_ctx(clock=clock, bundle=lambda: _bundle(unpaged)))
    assert res.status == "fail" and res.data["unpaged"] == ["payments"]

    fake = FakeLLMProvider()
    w = _wire(uow, clock, ids, aead, fake, catalog=unpaged)
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, SafeDefaulted)
    assert outcome.decision.reason == "no-paging-integration"
    assert fake.captured == [], "refused before the LLM is consulted"


# --- llm ---------------------------------------------------------------------


def test_doctor_llm_pass(clock: Any) -> None:
    res = LLMCheck().run(_ctx(clock=clock))
    assert res.status == "pass" and _MODEL in res.detail


def test_doctor_llm_fail(clock: Any) -> None:
    remapped = _LLM(resolve=lambda m: "claude-sonnet-4-5-20251101")
    res = LLMCheck().run(_ctx(clock=clock, llm=remapped))
    assert res.status == "fail" and "resolved to" in res.detail


# --- oidc --------------------------------------------------------------------


def test_doctor_oidc_pass(clock: Any) -> None:
    assert OIDCCheck().run(_ctx(clock=clock)).status == "pass"


def test_doctor_oidc_fail(clock: Any) -> None:
    res = Doctor([OIDCCheck()]).run(_ctx(clock=clock, identity=_DeadIdP())).results[0]
    assert res.status == "fail" and "connection refused" in res.detail


# --- runner ------------------------------------------------------------------


class _Fixed:
    def __init__(self, name: str, status: str) -> None:
        self.name, self._status = name, status

    def run(self, ctx: DoctorContext) -> CheckResult:
        return CheckResult(self.name, self._status, "x")  # type: ignore[arg-type]


class _Boom:
    name = "boom"

    def run(self, ctx: DoctorContext) -> CheckResult:
        raise RuntimeError("kaput")


def test_doctor_exit_codes(clock: Any) -> None:
    ctx = _ctx(clock=clock)
    assert Doctor([_Fixed("a", "pass"), _Fixed("b", "pass")]).run(ctx).exit_code == 0
    assert Doctor([_Fixed("a", "pass"), _Fixed("b", "warn")]).run(ctx).exit_code == 3
    assert Doctor([_Fixed("a", "warn"), _Fixed("b", "fail")]).run(ctx).exit_code == 1
    report = Doctor([_Boom(), _Fixed("after", "pass")]).run(ctx)
    assert [(r.name, r.status) for r in report.results] == [("boom", "fail"), ("after", "pass")]
    assert "kaput" in report.results[0].detail
    assert report.as_dict()["exit_code"] == 1
