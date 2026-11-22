"""WP-223: onboarding driven from the instance (ADR-0023 §2).

Canonical test IDs from 12-wp WP-223: ``test_autopilot_runs_as_worker_job``,
``test_manifest_edited_from_ui``. (``test_step_without_input_is_done_not_blocked``
and ``test_catalog_and_detectors_are_steps`` land with those steps.)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.onboarding_control import (
    STALE_RUN_S,
    OnboardingControl,
    OnboardingControlStore,
)
from supportagent.bootstrap import (
    _UnrecordedProvider,
    api_context_for,
    onboarding_control_for,
    run_autopilot,
    validate_manifest_text,
)
from supportagent.domain.errors import DomainValidationError
from supportagent.entrypoints.api.app import create_app
from supportagent.entrypoints.worker.jobs import AutopilotJob, JobResult
from tests.unit.api_support import ADA, MARKER, SECRET, TENANT, catalog, login
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

_MASTER_KEY = b"test" * 8


def _manifest(source: Path) -> str:
    return (
        "system:\n  id: shop\n  name: The shop\n"
        f"sources:\n  git:\n    - repo: file:{source}\n"
        '      paths: ["**/*.py", "**/*compose*.y*ml", "**/Dockerfile*"]\n'
    )


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "system"
    (root / "api").mkdir(parents=True)
    (root / "docker-compose.yml").write_text(
        "services:\n  api:\n    build: {context: ., dockerfile: api/Dockerfile}\n"
        "    depends_on: [db]\n  db:\n    image: postgres:16\n"
    )
    (root / "api" / "Dockerfile").write_text("FROM python:3.12\nCOPY api /app\n")
    (root / "api" / "app.py").write_text('QUERY = "SELECT id FROM orders"\n')
    return root


def _config(tmp_path: Path) -> Any:
    config = _shadow_config()
    return config.model_copy(
        update={
            "tenant": config.tenant.model_copy(update={"id": TENANT}),
            "onboarding": config.onboarding.model_copy(
                update={"workspace": str(tmp_path / "ws"), "autopilot_interval_h": 24}
            ),
        }
    )


def _control(db: Any, clock: Any, tenant: str) -> OnboardingControl:
    store = OnboardingControlStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=tenant)
    return OnboardingControl(store, clock, validate=validate_manifest_text)


def test_manifest_edited_from_ui(tmp_path: Path, db: Any, clock: Any, ids: Any) -> None:
    oidc = FakeOIDC()
    ctx = api_context_for(
        db,
        tenant_id=TENANT,
        catalog=catalog("payments"),
        master_key=_MASTER_KEY,
        clock=clock,
        ids=ids,
        identity=oidc,
        session_secret=SECRET,
        secure_cookies=False,
        config_marker=MARKER,
        validate_manifest=validate_manifest_text,
    )
    client = TestClient(create_app(ctx))
    login(client, oidc, ADA)
    assert "Describe the system" in client.get("/ui/onboarding").text, "where to start"
    page = client.get("/ui/onboarding/system").text
    assert "repo: file:/mnt/system" in page, "a starter manifest"
    token = client.cookies["sa_csrf"]

    pasted = _manifest(tmp_path).replace("      paths", "      token: ghp_secret\n      paths")
    refused = client.post("/ui/onboarding/system", data={"csrf_token": token, "manifest": pasted})
    assert refused.status_code == 422 and "environment variables" in refused.text
    assert ctx.onboarding_control is not None
    assert ctx.onboarding_control().manifest() is None, "a refused manifest isn't stored"

    saved = client.post(
        "/ui/onboarding/system",
        data={"csrf_token": token, "manifest": _manifest(tmp_path)},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].endswith("saved=shop")
    run = client.post("/ui/onboarding/run", data={"csrf_token": token}, follow_redirects=False)
    assert run.status_code == 303
    state = ctx.onboarding_control().state()
    assert state["system"] == "shop" and state["requested_by"]
    assert "run requested" in client.get("/ui/onboarding").text


def test_autopilot_runs_as_worker_job(tmp_path: Path, db: Any, clock: Any) -> None:
    config = _config(tmp_path)
    control = onboarding_control_for(config, db, clock)
    control.save_manifest(_manifest(_source(tmp_path)), actor="ada")
    control.request_run(actor="ada")

    claim = control.claim(interval_s=24 * 3600)
    assert claim is not None and claim.reason == "requested"
    assert control.claim(interval_s=24 * 3600) is None, "one run in flight"
    status = run_autopilot(
        config, engine=db, master_key=_MASTER_KEY, clock=clock, llm_provider=_UnrecordedProvider()
    )
    assert status is not None
    steps = {s.step: s.state for s in status.steps}
    assert steps["ingest"] == "done" and steps["graph"] == "done"
    assert (tmp_path / "ws" / "system.yaml").read_text() == _manifest(tmp_path / "system")
    state = control.state()
    assert state["finished_ts"] >= state["started_ts"] and "step(s)" in state["last_detail"]
    assert control.claim(interval_s=24 * 3600) is None, "nothing requested, not due"


def test_a_run_is_claimed_when_requested_due_or_stale(db: Any, clock: Any) -> None:
    control = _control(db, clock, "claims")
    assert control.claim(interval_s=60) is None, "no manifest, nothing to run"
    with pytest.raises(DomainValidationError, match="manifest"):
        control.request_run(actor="ada")
    control.save_manifest(_manifest(Path("/srv/shop")), actor="ada")
    first = control.claim(interval_s=3600)
    assert first is not None and first.reason == "scheduled", "never run: due"
    control.finish(ok=True, detail="ok")
    assert control.claim(interval_s=3600) is None
    clock.advance(3600)
    assert control.claim(interval_s=3600) is not None, "due again"
    clock.advance(STALE_RUN_S + 1)  # the worker died mid-run
    assert control.claim(interval_s=0) is None, "not requested and the schedule is off"
    control.request_run(actor="bob")
    again = control.claim(interval_s=0)
    assert again is not None and again.reason == "requested" and again.actor == "bob"


def test_the_job_runs_beside_the_loop_and_holds_its_lease() -> None:
    held: set[str] = set()
    started, release = threading.Event(), threading.Event()

    class _Lease:
        def acquire(self, *, name: str, owner: str, ttl_s: int) -> bool:
            if name in held:
                return False
            held.add(name)
            return True

        def release(self, *, name: str, owner: str) -> None:
            held.discard(name)

    def run(claim: object) -> None:
        started.set()
        release.wait(5)

    job = AutopilotJob(claim=lambda: "go", run=run, lease=_Lease(), owner="w1", ttl_s=60)
    assert job.tick() == JobResult(name="onboard.autopilot", ran=True, rows=1)
    assert started.wait(5)
    assert job.tick().ran is False, "never a second run while one is in flight"
    assert held == {"onboard.autopilot"}
    release.set()
    job.join(5)
    assert held == set(), "the lease is released when the run ends"
    idle = AutopilotJob(claim=lambda: None, run=run, lease=_Lease(), owner="w1", ttl_s=60)
    assert idle.tick() == JobResult(name="onboard.autopilot", ran=True, rows=0)
    assert held == set()


def test_an_installer_sets_the_system_without_a_browser(
    tmp_path: Path, db: Any, clock: Any
) -> None:
    from supportagent.bootstrap import onboard_system  # noqa: PLC0415
    from supportagent.domain.errors import ConfigError  # noqa: PLC0415

    config = _config(tmp_path)
    path = tmp_path / "system.yaml"
    path.write_text(_manifest(_source(tmp_path)))
    state = onboard_system(config, manifest=path, run=True, engine=db, clock=clock)
    assert state["system"] == "shop" and state["requested_by"] == "cli"
    path.write_text("system: {id: Not Valid}\n")
    with pytest.raises(ConfigError):
        onboard_system(config, manifest=path, engine=db, clock=clock)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_what_the_instance_cannot_read_is_a_need(tmp_path: Path, db: Any, clock: Any) -> None:
    config = _config(tmp_path)
    source = _source(tmp_path)
    private = source / "private"
    private.mkdir()
    (private / "x.py").write_text("x = 1\n")
    private.chmod(0)
    try:
        control = onboarding_control_for(config, db, clock)
        control.save_manifest(_manifest(source), actor="ada")
        status = run_autopilot(
            config,
            engine=db,
            master_key=_MASTER_KEY,
            clock=clock,
            llm_provider=_UnrecordedProvider(),
        )
    finally:
        private.chmod(0o700)
    assert status is not None
    access = [n for n in status.needs if n.kind == "access"]
    assert access and "can't be read" in access[0].what and "private" in access[0].what
