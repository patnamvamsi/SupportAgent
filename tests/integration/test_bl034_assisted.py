"""BL-034 / ADR-0008: assisted mode end to end through the worker.

A ServiceNow ticket is triaged by the real ``TriageService``; its work note
is covered by an approved T1 grant, so it's enqueued in the triage unit of
work, and ``outbox.execute`` writes it back to the instance with its marker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP, SyncASGITransport
from supportagent.bootstrap import build_rca_job, build_worker
from supportagent.config.schema import AppConfig
from supportagent.domain.errors import ConfigError
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.entrypoints.worker.jobs import COMMS_CADENCE_LEASE, OUTBOX_EXECUTE_LEASE
from supportagent.entrypoints.worker.loop import Worker
from tests.integration.test_shadow_worker import _config
from tests.integration.test_wp101_lifecycle import _SVC, _Ticket

_LABEL = "network/dns"


def _assisted(tmp_path: Path) -> AppConfig:
    config = _config(tmp_path)
    strong = CalibrationCountRow(n=50, correct=50)
    Path(config.calibration.path).write_text(
        CalibrationTable(
            grade="smoke",
            snapshot_id="snap-a",
            model_id=config.llm.slots["classify"].model_id,
            labels={_LABEL: CalibrationLabelRow(agree=strong, abstain=strong)},
        ).model_dump_json(),
        encoding="utf-8",
    )
    return config.model_copy(update={"mode": "assisted"})


def _seed_grant(db: Engine) -> None:
    with db.begin() as conn:
        for uid in ("user_ada", "user_bo"):
            conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, name, is_active, created_ts) "
                    "VALUES (:id, 'acme', :e, :id, 1, 0)"
                ),
                {"id": uid, "e": f"{uid}@acme.test"},
            )
        conn.execute(
            text(
                "INSERT INTO policy_grants (id, tenant_id, action_kind, scope, status, "
                "requested_by, requested_at_ts, granted_by, granted_at_ts, valid_to_ts) "
                "VALUES ('grnt_1', 'acme', 'work_note', 'service:payroll-api:work_note', "
                "'approved', 'user_ada', 0, 'user_bo', 0, 9999999999)"
            )
        )


@pytest.fixture
def sn() -> FakeServiceNowHTTP:
    fake = FakeServiceNowHTTP()
    fake.seed_row("sys_user", {"sys_id": "u-svc", "user_name": _SVC})
    return fake


def _worker(  # noqa: PLR0913, PLR0917
    config: AppConfig, db: Engine, clock: Any, ids: Any, key: bytes, sn: FakeServiceNowHTTP
) -> Worker:
    llm = FakeLLMProvider()
    llm.respond(
        '{"classification": "' + _LABEL + '", "severity": "P3", "summary": "x", "confidence": 0.9}'
    )
    return build_worker(
        config,
        master_key=key,
        clock=clock,
        ids=ids,
        transport=SyncASGITransport(sn.app),
        llm_provider=llm,
        engine=db,
    )


def test_assisted_worker_sends_a_grant_covered_work_note(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> None:
    _seed_grant(db)
    worker = _worker(_assisted(tmp_path), db, clock, ids, master_key, sn)
    names = [job.name for job in worker.jobs]
    assert OUTBOX_EXECUTE_LEASE in names
    assert COMMS_CADENCE_LEASE in names
    _Ticket(sn, clock)
    worker.run_once()  # poll -> triage -> grant-covered proposal enqueued (BL-024)
    worker.run_once()  # outbox.execute drains it
    with db.connect() as conn:
        outbox = conn.execute(text("SELECT status, external_ref FROM outbox")).all()
        proposals = {r[0] for r in conn.execute(text("SELECT status FROM proposals")).all()}
        shadow = conn.execute(text("SELECT COUNT(*) FROM shadow_records")).scalar_one()
    assert [r[0] for r in outbox] == ["done"]
    assert proposals == {"executed"}
    assert shadow == 0  # assisted mode triages for real; nothing is counterfactual
    notes = [
        str(j["value"])
        for j in sn._journal
        if j["element_id"] == "sys-1" and j["element"] == "work_notes"
    ]
    assert len(notes) == 1
    assert notes[0].splitlines()[-1].startswith("[sa:")


def test_shadow_worker_schedules_no_sends(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> None:
    worker = _worker(_config(tmp_path), db, clock, ids, master_key, sn)
    names = [job.name for job in worker.jobs]
    assert OUTBOX_EXECUTE_LEASE not in names
    assert COMMS_CADENCE_LEASE not in names


def test_assisted_needs_automation_actors_decided(  # noqa: PLR0913, PLR0917
    tmp_path: Path, db: Engine, clock: Any, ids: Any, master_key: bytes, sn: FakeServiceNowHTTP
) -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO automation_actors (tenant_id, sys_user, kind) "
                "VALUES ('acme', 'flow.designer', 'automation')"
            )
        )
    with pytest.raises(ConfigError, match="undecided"):
        _worker(_assisted(tmp_path), db, clock, ids, master_key, sn)


def test_assisted_rca_gate_enqueues_grant_covered_proposals(
    tmp_path: Path, db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    """BL-040: RCA's page note is grant-covered, so its gate must reach the outbox."""
    config = _assisted(tmp_path)
    job = build_rca_job(
        config,
        engine=db,
        master_key=master_key,
        clock=clock,
        ids=ids,
        llm_provider=FakeLLMProvider(),
    )
    assert job._gate is not None
    assert job._gate._outbox is not None  # type: ignore[attr-defined]
