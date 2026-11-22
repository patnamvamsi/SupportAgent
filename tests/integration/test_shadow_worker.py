"""The shadow worker end to end (02 §7, 10-wp WP-008/014; BL-011, BL-014).

``bootstrap.build_worker`` wires the real ``ServiceNowSource`` (over
``FakeServiceNowHTTP``), ``SyncService``, ``ShadowInbound`` and the shadow
runner on the test database. One ``run_once`` polls ServiceNow under the
``sync.poll.incident`` lease: a new incident becomes a shadow record, a human
change on it becomes a ``sync`` event on the case's trace, and the agent's own
echo is dropped. Nothing is ever proposed, approved or sent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.bootstrap import build_worker
from supportagent.config.schema import AppConfig
from supportagent.domain.model.calibration import CalibrationTable
from supportagent.entrypoints.worker.jobs import JobResult
from supportagent.entrypoints.worker.loop import Worker
from tests.unit.test_triage_service import _shadow_config

_MODEL = "claude-sonnet-4-5-20250929"
_POLL = "sync.poll.incident"


def _config(tmp_path: Path) -> AppConfig:
    calibration = tmp_path / "calibration.json"
    calibration.write_text(
        CalibrationTable(
            grade="smoke", snapshot_id="snap-w", model_id=_MODEL, labels={}
        ).model_dump_json(),
        encoding="utf-8",
    )
    data = _shadow_config().model_dump(mode="json")
    data["paths"]["catalog"] = "catalog/services.example.yaml"
    data["calibration"]["path"] = str(calibration)
    data["servicenow"]["auth"] = {"kind": "basic", "user": "svc", "password": "pw-fake"}
    for secret in ("client_id", "client_secret", "session_secret"):
        data["identity"][secret] = "w-" + secret + "-" + "x" * 32
    return AppConfig.model_validate(data)


@pytest.fixture
def sn() -> FakeServiceNowHTTP:
    return FakeServiceNowHTTP()


@pytest.fixture
def worker(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> Worker:
    return build_worker(
        _config(tmp_path),
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(sn.app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )


def _polls(worker: Worker) -> list[JobResult]:
    """The ``sync.poll`` results of one round (other jobs, like ``groups.close``, also run)."""
    return [r for r in worker.run_once() if r.name.startswith("sync.poll.")]


def _scalar(db: Engine, sql: str, **params: object) -> Any:
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def _no_side_effects(db: Engine) -> None:
    for table in ("proposals", "approvals", "outbox"):
        assert _scalar(db, f"SELECT COUNT(*) FROM {table}") == 0, table  # noqa: S608


def test_worker_polls_new_incident_into_shadow(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    sn.seed_record(
        "incident",
        "sys-1",
        number="INC0010001",
        short_description="core-db connection pool exhausted",
        description="ORA-12519 on every request",
        priority="2",
        cmdb_ci="CI2002",
        caller_id="u-caller",
        updated_at=clock.now(),
        updated_by="alice",
    )
    assert _polls(worker) == [JobResult(name=_POLL, ran=True, rows=1)]

    assert _scalar(db, "SELECT COUNT(*) FROM shadow_records") == 1
    case_id = _scalar(db, "SELECT case_id FROM shadow_records")
    assert _scalar(db, "SELECT external_id FROM cases WHERE id = :id", id=case_id) == "sys-1"
    assert _scalar(db, "SELECT COUNT(*) FROM leases") == 0, "the lease is released after the run"
    assert _scalar(db, "SELECT COUNT(*) FROM sync_cursors") == 1
    _no_side_effects(db)

    # Nothing new: the overlap window re-reads sys-1, and sync_seen dedups it.
    assert _polls(worker) == [JobResult(name=_POLL, ran=True, rows=0)]
    assert _scalar(db, "SELECT COUNT(*) FROM shadow_records") == 1


def test_worker_records_human_changes_and_drops_own_echo(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    sn.seed_record(
        "incident",
        "sys-1",
        number="INC0010001",
        short_description="payroll-api 500s",
        priority="3",
        cmdb_ci="CI3003",
        caller_id="u-caller",
        updated_at=clock.now(),
        updated_by="alice",
    )
    # A record the integration user created is our own, never an inbound alert.
    sn.seed_record(
        "incident",
        "sys-own",
        number="INC0010002",
        short_description="created by the agent",
        caller_id="u-caller",
        updated_at=clock.now(),
        updated_by="svc_supportagent",
    )
    assert _polls(worker) == [JobResult(name=_POLL, ran=True, rows=1)]
    trace_id = _scalar(db, "SELECT trace_id FROM cases WHERE external_id = 'sys-1'")
    assert _scalar(db, "SELECT COUNT(*) FROM cases WHERE external_id = 'sys-own'") == 0

    clock.advance(60)
    sn.seed_record(
        "incident",
        "sys-1",
        number="INC0010001",
        short_description="payroll-api 500s",
        priority="2",
        cmdb_ci="CI3003",
        caller_id="u-caller",
        updated_at=clock.now(),
        updated_by="bob",
        revision=2,
    )
    sn.seed_audit(
        "incident",
        "sys-1",
        user="bob",
        ts=clock.now(),
        fieldname="priority",
        oldvalue="3",
        newvalue="2",
        checkpoint=2,
    )
    assert _polls(worker) == [JobResult(name=_POLL, ran=True, rows=1)]
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM events WHERE kind = 'sync' AND trace_id = :t",
            t=trace_id,
        )
        == 1
    ), "the change is recorded on the case's own trace"
    assert _scalar(db, "SELECT COUNT(*) FROM shadow_records") == 1, "a change is not a new case"
    _no_side_effects(db)


def test_worker_skips_when_another_replica_holds_the_lease(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    until = int(clock.now().timestamp()) + 600
    with db.begin() as conn:
        conn.execute(
            text("INSERT INTO leases (name, owner, until_ts) VALUES (:n, 'other', :u)"),
            {"n": _POLL, "u": until},
        )
    sn.seed_record(
        "incident",
        "sys-1",
        number="INC0010001",
        short_description="x",
        caller_id="u-caller",
        updated_at=clock.now(),
    )
    assert _polls(worker) == [JobResult(name=_POLL, ran=False)]
    assert _scalar(db, "SELECT COUNT(*) FROM cases") == 0
    assert _scalar(db, "SELECT owner FROM leases WHERE name = :n", n=_POLL) == "other"


def test_worker_survives_a_servicenow_outage(
    worker: Worker, db: Engine, clock: Any, sn: FakeServiceNowHTTP
) -> None:
    sn.queue_status(400, body={"error": {"message": "bad query"}})  # non-retryable
    assert _polls(worker) == [], "the failed poll is logged, not raised"
    assert _scalar(db, "SELECT COUNT(*) FROM leases") == 0, "released even on error"


@pytest.mark.parametrize("ticketless", [False, True], ids=["with-servicenow", "alerts-only"])
def test_worker_drains_webhooks_into_shadow(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    db: Engine,
    clock: Any,
    ids: Any,
    master_key: bytes,
    sn: FakeServiceNowHTTP,
    ticketless: bool,
) -> None:
    """WP-113 through the worker: a verified delivery is triaged by the shadow runner.

    ADR-0022: alerts-only, with no ticket connector, nothing polls a ticket system.
    """
    import json  # noqa: PLC0415

    from supportagent.adapters.fakes.oidc import FakeOIDC  # noqa: PLC0415
    from supportagent.bootstrap import api_context_for  # noqa: PLC0415
    from supportagent.config.schema import Webhook  # noqa: PLC0415
    from supportagent.domain.webhook import signature  # noqa: PLC0415

    secret = "nagios-webhook-secret-0123456789abcdef"  # noqa: S105 — a test secret
    mapping = {"external_id": "$.id", "subject": "$.title", "resource": "$.host"}
    hook = Webhook(source="nagios", secret=secret, mapping=mapping)  # type: ignore[arg-type]
    update: dict[str, object] = {"webhooks": [hook]}
    if ticketless:
        update["servicenow"] = None
    cfg = AppConfig.model_validate({**_config(tmp_path).model_dump(), **update})
    worker = build_worker(
        cfg,
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(sn.app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )
    from supportagent.app.webhooks import WebhookSource  # noqa: PLC0415

    ctx = api_context_for(
        db,
        tenant_id="acme",
        catalog=_catalog_of(cfg),
        master_key=master_key,
        clock=clock,
        ids=ids,
        identity=FakeOIDC(),
        session_secret="s" * 32,
        secure_cookies=False,
        config_marker="config:x",
        webhook_sources={"nagios": WebhookSource(secret=secret.encode(), mapping=mapping)},
    )
    body = json.dumps({"id": "evt-1", "title": "core-db down", "host": "db-pool-01"}).encode()
    stamp = str(int(clock.now().timestamp()))
    ctx.webhooks().receive(
        "nagios",
        {
            "X-SA-Timestamp": stamp,
            "X-SA-Nonce": "n1",
            "X-SA-Signature": signature(secret.encode(), stamp, body),
        },
        body,
    )
    results = worker.run_once()
    drained = [r for r in results if r.name == "webhooks.drain"]
    assert drained == [JobResult(name="webhooks.drain", ran=True, rows=1)]
    polls = [r.name for r in results if r.name.startswith("sync.poll.")]
    assert polls == ([] if ticketless else [_POLL])
    assert _scalar(db, "SELECT COUNT(*) FROM shadow_records") == 1
    assert _scalar(db, "SELECT source FROM cases") == "webhook:nagios", (
        "the alert type defaults to the source name"
    )
    assert _scalar(db, "SELECT COUNT(*) FROM webhook_inbox WHERE processed_ts IS NULL") == 0


def _catalog_of(cfg: AppConfig) -> Any:
    from supportagent.adapters.files.catalog_loader import load_catalog  # noqa: PLC0415

    return load_catalog(Path(cfg.paths.catalog))
