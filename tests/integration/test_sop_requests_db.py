"""ADR-0014 part 1 on the database: the ``rca.run`` job proposes SOP steps through the gate.

A SUPPORTED cause with an applicable SOP yields a proposal per step, in the
artifact's UoW, carrying the SOP's provenance and the artifact id. Its scope
is the SOP's own, so even a service-wide ``work_note`` setup grant doesn't
cover it: the step waits for a human approval and nothing is enqueued.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, text

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import SqlKeyStore
from supportagent.adapters.db.repos.events import SqlEventSink
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.app.gate import GateService
from supportagent.app.rca import BufferedEvents, DocPolicy, Knowledge, RcaJob, RcaService
from supportagent.domain.kinds import ActionKind
from supportagent.domain.knowledge import DocMeta, KnowledgeIndex, chunk_document
from supportagent.domain.model.catalog import Catalog
from supportagent.domain.model.runbook import RcaBudget
from supportagent.domain.slots import SlotName
from supportagent.domain.sop import Sop, SopStep
from tests.integration.test_bl034_assisted import _seed_grant
from tests.integration.test_wp110_rca import _ROUTED
from tests.unit.test_rca import (
    FAILED_RUN,
    POOL_LOGS,
    ROOT,
    SERVICE,
    FakeTools,
    ScriptedLLM,
    _knowledge,
)

_SOP = Sop(
    when=("recent_change_push",),
    steps=(SopStep("roll-back", ActionKind.WORK_NOTE, "Ask the deployer to roll back."),),
)


def _docs() -> KnowledgeIndex:
    meta = DocMeta(
        doc_id="wiki:deploy-sop.md",
        space="wiki",
        title="Bad deploy SOP",
        version="3",
        sop=_SOP,
    )
    return KnowledgeIndex(chunk_document(meta, "# Bad deploy\nRoll back first.", redact=str))


def _job(
    db: Engine, master_key: bytes, clock: Any, ids: Any, *, paging: str | None = None
) -> RcaJob:
    uow = SqlUnitOfWork(db)
    aead = Aead(master_key=master_key, keys=SqlKeyStore(uow), clock=clock)
    events = SqlEventSink(uow=uow, cipher=aead)
    buffer = BufferedEvents()
    kb = _knowledge(ROOT / "runbooks")
    catalog = Catalog(
        version="1",
        services=[SERVICE.model_copy(update={"docs": ["wiki"], "paging_integration": paging})],
    )
    service = RcaService(
        knowledge=Knowledge(kb.runbooks, kb.probes, kb.excludes),
        tools=FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS}),
        llm=ScriptedLLM({SlotName.ENTAIL: lambda _: {"verdict": "supports"}}),
        events=buffer,
        ids=ids,
        clock=clock,
        prior={},
        docs=_docs(),
        doc_policy=DocPolicy(),
    )
    gate = GateService(
        uow=uow, clock=clock, ids=ids, events=events, catalog=catalog, ttl=timedelta(hours=4)
    )
    return RcaJob(
        uow=uow,
        service=service,
        catalog=catalog,
        buffer=buffer,
        events=events,
        lite_routes={"db-saturation"},
        default_budget=RcaBudget(),
        ids=ids,
        clock=clock,
        tenant_id="acme",
        gate=gate,
    )


def test_sop_step_proposed_for_approval_despite_a_work_note_grant(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    _seed_grant(db)  # service:payroll-api:work_note, approved
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, classification, "
                "severity, status, decision_json, opened_ts, last_alert_ts, trace_id, "
                "external_table, external_id) "
                "VALUES ('case_1', 'acme', 'fp-1', 'alert', 'payroll-api', "
                "'database/saturation', 'P2', 'triaged', :d, 100, 100, 'tr-1', 'incident', 'sys-1')"
            ),
            {"d": _ROUTED},
        )

    assert _job(db, master_key, clock, ids).run_once() == 1

    with db.connect() as conn:
        [artifact] = conn.execute(text("SELECT id, artifact_json FROM rca_artifacts")).all()
        proposals = conn.execute(
            text("SELECT action_kind, scope, status, payload_json FROM proposals")
        ).all()
        outbox = conn.execute(text("SELECT COUNT(*) FROM outbox")).scalar_one()
    steps = json.loads(artifact[1])["sop_steps"]
    assert [(s["doc"], s["step"], s["hash"]) for s in steps] == [
        ("wiki:deploy-sop.md", "roll-back", _SOP.digest)
    ]
    [(kind, scope, status, payload_json)] = proposals
    assert kind == "work_note" and status == "pending_approval"
    assert scope == f"sop:wiki:deploy-sop.md@{_SOP.digest}:roll-back:work_note"
    payload = json.loads(payload_json)
    assert payload["rca_artifact"] == artifact[0] and payload["sop_step"] == "roll-back"
    assert "Ask the deployer to roll back." in payload["text"]
    assert outbox == 0  # nothing auto-approved, nothing enqueued


def test_rca_result_attached_to_the_open_page_from_allowlisted_fields(
    db: Engine, master_key: bytes, clock: Any, ids: Any
) -> None:
    """BL-040: one ``enrich_page`` after RCA, with the outcome, ids and tools only."""
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, service, classification, "
                "severity, status, decision_json, opened_ts, last_alert_ts, trace_id, "
                "external_table, external_id) "
                "VALUES ('case_2', 'acme', 'fp-2', 'alert', 'payroll-api', "
                "'database/saturation', 'P2', 'triaged', :d, 100, 100, 'tr-2', 'incident', 'sys-2')"
            ),
            {"d": _ROUTED},
        )
    job = _job(db, master_key, clock, ids, paging="pagerduty:payroll")
    assert job.run_once() == 1
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT action_kind, scope, payload_json FROM proposals ORDER BY action_kind")
        ).all()
    page = [r for r in rows if r[0] == "enrich_page"]
    [(_, scope, payload_json)] = page
    assert scope == "service:payroll-api:enrich_page"
    payload = json.loads(payload_json)
    assert payload["template_id"] == "enrich_page_rca_v1"
    assert payload["integration"] == "pagerduty:payroll" and payload["source_ref"] == "sys-2"
    assert payload["text"].startswith("SupportAgent RCA for case case_2: SUPPORTED.")
    assert "Cause recent_change_push, confirmed by gha.recent_runs." in payload["text"]
    assert "1 SOP step(s) await approval" in payload["text"]
