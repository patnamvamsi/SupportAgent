"""BL-028: ``proposals.expire`` and ``retention.sweep`` (02 §7, 03 §2.4), on both engines."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.repos.retention import SqlRetention
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.app.retention import RetentionService
from supportagent.bootstrap import build_worker
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event
from supportagent.entrypoints.worker.jobs import PROPOSALS_EXPIRE_LEASE, RETENTION_SWEEP_LEASE
from tests.integration.test_shadow_worker import _config
from tests.unit.api_support import TENANT, seed_proposal


def _count(db: Engine, sql: str) -> int:
    with db.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())


def _event(sink: SqlEventSink, event_id: str, ts: Any) -> None:
    sink.append(
        Event(
            id=event_id,
            tenant_id=TENANT,
            trace_id="tr_x",
            span_id=event_id,
            parent_span_id=None,
            kind=EventKind.SYNC,
            actor="agent",
            payload={"v": 1},
            ts=ts,
        )
    )


def test_retention_sweep_deletes_only_expired_rows(
    db: Engine, clock: Any, ids: Any, master_key: bytes
) -> None:
    seed_proposal(db, proposal_id="p_old", case_id="case_1", created_at_ts=0)
    uow = SqlUnitOfWork(db)
    sink = SqlEventSink(
        uow=uow, cipher=Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    )
    now = clock.now()
    old_s = int((now - timedelta(days=400)).timestamp())
    new_s = int((now - timedelta(days=1)).timestamp())
    with uow:
        _event(sink, "evt_old", now - timedelta(days=400))
        _event(sink, "evt_new", now - timedelta(days=1))
        for alert_id, ts in (("alrt_old", old_s), ("alrt_new", new_s)):
            uow.connection.execute(
                text("INSERT INTO blobs (key, tenant_id, data) VALUES (:k, :t, :d)"),
                {"k": f"blob:{alert_id}", "t": TENANT, "d": b"raw"},
            )
            uow.connection.execute(
                text(
                    "INSERT INTO alerts (id, tenant_id, case_id, adapter, external_id, "
                    "payload_ref, received_ts) VALUES (:id, :t, 'case_1', 'servicenow', :id, "
                    ":k, :ts)"
                ),
                {"id": alert_id, "t": TENANT, "k": f"blob:{alert_id}", "ts": ts},
            )
        uow.commit()

    service = RetentionService(
        uow=uow,
        store=SqlRetention(uow),
        events=sink,
        ids=ids,
        clock=clock,
        tenant_id=TENANT,
        events_days=395,
        alerts_days=90,
        comms_days=395,
    )
    counts = service.sweep()
    assert counts["events"] == 1
    assert (counts["alerts"], counts["blobs"]) == (1, 1)
    assert _count(db, "SELECT COUNT(*) FROM events WHERE id = 'evt_new'") == 1
    assert _count(db, "SELECT COUNT(*) FROM alerts") == 1
    assert _count(db, "SELECT COUNT(*) FROM blobs WHERE key = 'blob:alrt_new'") == 1
    # Audit tables are never swept, however old.
    assert _count(db, "SELECT COUNT(*) FROM proposals WHERE id = 'p_old'") == 1
    # The summary is a decision event written after the delete: it survives the sweep.
    assert _count(db, "SELECT COUNT(*) FROM events WHERE trace_id LIKE 'retention-%'") == 1
    assert service.sweep()["events"] == 0


def test_worker_schedules_expire_and_retention(
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes
) -> None:
    worker = build_worker(
        _config(tmp_path),
        master_key=master_key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(FakeServiceNowHTTP().app),
        llm_provider=FakeLLMProvider(),
        engine=db,
    )
    names = [job.name for job in worker.jobs]
    assert PROPOSALS_EXPIRE_LEASE in names
    assert RETENTION_SWEEP_LEASE in names
    seed_proposal(db, proposal_id="p_due", case_id="case_2")
    with db.begin() as conn:
        conn.execute(text("UPDATE proposals SET expires_ts = 1 WHERE id = 'p_due'"))
    results = {r.name: r for r in worker.run_once()}
    assert results[PROPOSALS_EXPIRE_LEASE].rows == 1
    with db.connect() as conn:
        status = conn.execute(text("SELECT status FROM proposals WHERE id = 'p_due'")).scalar_one()
    assert status == "expired"
