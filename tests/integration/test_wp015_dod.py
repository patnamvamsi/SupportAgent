"""WP-015 DoD: ``doctor`` against FakeServiceNow plus the example config is all green in < 10 s.

``supportagent.example.yaml`` is loaded through the real loader, with its
``*_env`` secrets from a test environment. Only two paths are swapped:
the example catalog (``catalog/services.yaml`` is customer-owned and not in
the repo) and a smoke calibration artifact (``evals/artifacts/`` is written
by WP-016's eval run). ServiceNow is ``FakeServiceNowHTTP`` behind the real
OAuth client and Table API probe. The LLM is a fake provider behind the real
``LLMClient.probe``, and the IdP is FakeOIDC.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.app.doctor import Doctor, default_checks
from supportagent.bootstrap import build_doctor_context
from supportagent.config.loader import load_config
from supportagent.config.schema import AppConfig
from supportagent.domain.model.calibration import CalibrationTable
from tests.unit.doctor_support import healthy_instance

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "claude-sonnet-4-5-20250929"
_ENV = {
    "SUPPORTAGENT_DB_URL": "sqlite://",
    "SN_CLIENT_ID": "sn-client",
    "SN_CLIENT_SECRET": "sn-secret-fake",
    "OIDC_CLIENT_ID": "oidc-client",
    "OIDC_CLIENT_SECRET": "oidc-secret-fake",
    "SA_SESSION_SECRET": "s" * 32,
    "ANTHROPIC_API_KEY": "sk-test-fake",
    "PAGERDUTY_TOKEN": "pd-token-fake",
}


class _ResolvingLLM(FakeLLMProvider):
    """Serves exactly the requested snapshot, as a pinned provider does."""

    def complete_json(self, request: Any) -> Any:
        response = super().complete_json(request)
        return dataclasses.replace(response, resolved_model=request.model_id)


def example_config(tmp_path: Path) -> AppConfig:
    calibration = tmp_path / "calibration.json"
    calibration.write_text(
        CalibrationTable(
            grade="smoke", snapshot_id="snap", model_id=_MODEL, labels={}
        ).model_dump_json(),
        encoding="utf-8",
    )
    text_ = (_REPO / "supportagent.example.yaml").read_text(encoding="utf-8")
    text_ = text_.replace(
        "catalog: catalog/services.yaml", "catalog: catalog/services.example.yaml"
    )
    text_ = text_.replace(
        "path: evals/artifacts/calibration.json", f"path: {calibration.as_posix()}"
    )
    path = tmp_path / "supportagent.yaml"
    path.write_text(text_, encoding="utf-8")
    return load_config(path, _ENV)


def test_wp015_dod_doctor_all_green_on_example_config(
    tmp_path: Path, db: Engine, clock: Any
) -> None:
    sn = healthy_instance(FakeServiceNowHTTP())
    ctx = build_doctor_context(
        example_config(tmp_path),
        clock=clock,
        engine=db,
        transport=SyncASGITransport(sn.app),
        llm_provider=_ResolvingLLM(),
        identity=FakeOIDC(),
        discover_automation=True,
    )
    started = time.monotonic()
    report = Doctor(default_checks()).run(ctx)
    elapsed = time.monotonic() - started

    statuses = {r.name: (r.status, r.detail) for r in report.results}
    failing = {k: v for k, v in statuses.items() if v[0] != "pass"}
    assert not failing, failing
    assert [r.name for r in report.results] == [
        "config",
        "packs",
        "knowledge",
        "db",
        "servicenow.auth",
        "servicenow.journal_visibility",
        "servicenow.integration_role",
        "servicenow.rate_limit",
        "servicenow.history_access",
        "servicenow.audit_retention",
        "automation_actors",
        "paging",
        "llm",
        "mcp",
        "oidc",
    ], "every 02 §4.1 v0.0 check runs, in table order"
    assert report.exit_code == 0
    assert elapsed < 10, f"doctor took {elapsed:.1f}s"
    assert sn.token_requests == 1, "the OAuth token is fetched once and reused"


def test_doctor_never_writes_to_servicenow(tmp_path: Path, db: Engine, clock: Any) -> None:
    sn = healthy_instance(FakeServiceNowHTTP())
    sn.seed_audit(
        "incident",
        "inc-1",
        user="flow.designer",
        ts=clock.now(),
        fieldname="assigned_to",
        newvalue="u2",
        checkpoint=2,
    )
    sn.seed_row(
        "sys_user",
        {
            "sys_id": "u-flow",
            "user_name": "flow.designer",
            "web_service_access_only": "false",
            "internal_integration_user": "true",
        },
    )
    ctx = build_doctor_context(
        example_config(tmp_path),
        clock=clock,
        engine=db,
        transport=SyncASGITransport(sn.app),
        llm_provider=_ResolvingLLM(),
        identity=FakeOIDC(),
        discover_automation=True,
    )
    report = Doctor(default_checks()).run(ctx)
    assert sn.patch_count == 0, "doctor performed a ServiceNow write"
    # The automation actor went to *our* database, unconfirmed.
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT sys_user, kind, confirmed_by FROM automation_actors")
        ).all()
    assert [tuple(r) for r in rows] == [("flow.designer", "integration", None)]
    actors = next(r for r in report.results if r.name == "automation_actors")
    assert actors.status == "pass"
    assert actors.data["added"] == 1
