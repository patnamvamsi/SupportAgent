"""WP-110 on the database: the ``rca.run`` job (ARCH §5).

Open cases routed to an ``rca: lite`` route get exactly one artifact; a
storm group gets one RCA (its lead); RCA events land with the artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.app.rca import BufferedEvents, RcaJob, RcaService
from supportagent.bootstrap import build_worker
from supportagent.config.schema import AppConfig
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.slots import SlotName
from supportagent.entrypoints.worker.jobs import RCA_RUN_LEASE
from tests.integration.test_shadow_worker import _config
from tests.unit.test_rca import (
    FAILED_RUN,
    POOL_LOGS,
    ROOT,
    SERVICE,
    FakeTools,
    ScriptedLLM,
    _knowledge,
)

_ROUTED = json.dumps({"outcome": "routed", "route_id": "db-saturation"})
_GENERIC = json.dumps({"outcome": "routed", "route_id": "generic"})


def _insert_case(  # noqa: PLR0913 — one column per argument
    db: Engine,
    case_id: str,
    *,
    opened: int,
    decision: str = _ROUTED,
    group: str | None = None,
    status: str = "triaged",
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, classification, "
                "severity, status, decision_json, group_id, opened_ts, last_alert_ts, trace_id) "
                "VALUES (:id, 'acme', :fp, 'alert', 'payroll-api', 'database/saturation', 'P2', "
                ":st, :d, :g, :ts, :ts, :tr)"
            ),
            {
                "id": case_id,
                "fp": f"fp-{case_id}",
                "st": status,
                "d": decision,
                "g": group,
                "ts": opened,
                "tr": f"tr-{case_id}",
            },
        )


def _job(db: Engine, master_key: bytes, clock: Any, ids: Any, tools: FakeTools) -> RcaJob:
    uow = SqlUnitOfWork(db)
    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    buffer = BufferedEvents()
    service = RcaService(
        knowledge=_knowledge(ROOT / "runbooks"),
        tools=tools,
        llm=ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": "supports"}}),
        events=buffer,
        ids=ids,
        clock=clock,
        prior={},
    )
    return RcaJob(
        uow=uow,
        service=service,
        catalog=Catalog(version="1", services=[SERVICE]),
        buffer=buffer,
        events=SqlEventSink(uow=uow, cipher=aead),
        lite_routes={"db-saturation"},
        default_budget=RcaBudget(),
        ids=ids,
        clock=clock,
        tenant_id="acme",
    )


def test_rca_job_one_artifact_per_group_lead(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO case_groups (id, tenant_id, opened_ts, rule, member_count) "
                "VALUES ('grp_1', 'acme', 100, 'storm', 2)"
            )
        )
    _insert_case(db, "case_lead", opened=100, group="grp_1")
    _insert_case(db, "case_member", opened=101, group="grp_1")
    _insert_case(db, "case_solo", opened=102)
    _insert_case(db, "case_generic", opened=103, decision=_GENERIC)
    _insert_case(db, "case_done", opened=104, status="resolved")
    tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
    job = _job(db, master_key, clock, ids, tools)

    assert job.run_once() == 2
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT case_id, outcome, artifact_json FROM rca_artifacts ORDER BY case_id")
        ).all()
        rca_events = conn.execute(
            text(
                "SELECT trace_id, kind FROM events WHERE kind IN ('rca_step', 'decision') "
                "ORDER BY trace_id, kind"
            )
        ).all()
    assert [(r[0], r[1]) for r in rows] == [
        ("case_lead", "SUPPORTED"),
        ("case_solo", "SUPPORTED"),
    ]
    artifact = json.loads(rows[0][2])
    assert artifact["plan"] == "runbook:db-pool"
    assert artifact["claims"][0]["sendable"] is True
    assert ("tr-case_lead", "rca_step") in [tuple(r) for r in rca_events]
    # Idempotent: a second run finds nothing left to do.
    assert job.run_once() == 0
    assert tools.calls == ["gha.recent_runs", "gha.recent_runs"]


def test_worker_schedules_rca_only_for_lite_routes(
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes
) -> None:
    def names(rca: str) -> list[str]:
        config = _config(tmp_path)
        data = config.model_dump(mode="json")
        data["routing"][0]["rca"] = rca
        for secret in ("client_id", "client_secret", "session_secret"):
            data["identity"][secret] = config.identity.model_dump()[secret].get_secret_value()
        worker = build_worker(
            AppConfig.model_validate(data),
            master_key=master_key,
            clock=clock,
            ids=ids,
            transport=SyncASGITransport(FakeServiceNowHTTP().app),
            llm_provider=FakeLLMProvider(),
            engine=db,
        )
        return [job.name for job in worker.jobs]

    assert RCA_RUN_LEASE not in names("none")
    assert RCA_RUN_LEASE in names("lite")
