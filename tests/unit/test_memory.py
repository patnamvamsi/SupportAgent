"""WP-108: case memory and retrieval (ARCH §5.2, §5.3).

Canonical test IDs from 11-wp §WP-108: ``test_similar_returns_empty_when_no_match``,
``test_signature_match_ranks_first``, ``test_similar_edges_derived_from_signature_equality``,
``test_citation_must_reference_existing_case``, ``test_memory_tenant_isolated``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.llm.detectors import built_in_detectors
from supportagent.adapters.llm.redact import Redactor
from supportagent.app.gate import GateService
from supportagent.app.memory import CaseMemory
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.kinds import ActionKind
from supportagent.domain.memory import MemoryDoc, bm25_scores, rank
from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.triage import Triaged
from supportagent.domain.trace import TraceContext
from tests.builders import an_alert
from tests.unit.api_support import catalog
from tests.unit.test_triage_service import _evt, _llm_json, _wire

_REDACTOR = Redactor(detectors=built_in_detectors())


def _memory(db: Any, clock: Any, tenant: str = "acme") -> CaseMemory:
    return CaseMemory(
        uow=SqlUnitOfWork(db),
        clock=clock,
        tenant_id=tenant,
        redact=lambda t: _REDACTOR.redact(t).text,
    )


def _case(db: Any, case_id: str, tenant: str = "acme", service: str = "payments") -> None:
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts, service, classification, severity) "
                "VALUES (:id, :t, :fp, 'alert', 'resolved', :tr, 0, 1, 1, :svc, 'payments/api', "
                "'P3')"
            ),
            {
                "id": case_id,
                "t": tenant,
                "fp": f"fp_{case_id}",
                "tr": f"tr_{case_id}",
                "svc": service,
            },
        )


def _alert(subject: str, body: str, signature: str | None, tenant: str = "acme") -> AlertEvent:
    return AlertEvent.model_validate(
        an_alert(tenant_id=tenant, subject=subject, body=body, error_signature=signature)
    )


def _remember(  # noqa: PLR0913
    mem: CaseMemory,
    db: Any,
    case_id: str,
    evt: AlertEvent,
    *,
    resolved: bool = True,
    service: str = "payments",
) -> None:
    _case(db, case_id, evt.tenant_id, service)
    mem.remember(case_id, evt, service=service, classification="payments/api", severity="P3")
    if resolved:
        mem.index_resolved(case_id)


def test_similar_returns_empty_when_no_match(db: Any, clock: Any) -> None:
    mem = _memory(db, clock)
    _remember(mem, db, "case_old", _alert("Disk full on billing", "volume /var at 99%", "disk:var"))
    _remember(
        mem,
        db,
        "case_new",
        _alert("Payment API timeouts", "gateway 504s", "5xx:gateway"),
        resolved=False,
    )
    assert mem.similar("case_new") == [], "same service alone is not a similar case"
    assert mem.similar("case_unknown") == []


def test_signature_match_ranks_first(db: Any, clock: Any) -> None:
    mem = _memory(db, clock)
    _remember(
        mem,
        db,
        "case_text",
        _alert(
            "Payment API 5xx spike again", "Payment API 5xx spike on checkout", "5xx:/v1/refunds"
        ),
    )
    _remember(
        mem,
        db,
        "case_sig",
        _alert("Checkout errors", "Customers report failures", "5xx:/v1/payments"),
    )
    _remember(
        mem,
        db,
        "case_new",
        _alert("Payment API 5xx spike", "5xx on /v1/payments", "5xx:/v1/payments"),
        resolved=False,
    )
    cited = mem.similar("case_new", k=3)
    assert [c.case_id for c in cited] == ["case_sig", "case_text"]
    assert cited[0].components["signature_match"] == 1.0
    assert cited[1].components["signature_match"] == 0.5, "same error family (5xx)"


def test_similar_edges_derived_from_signature_equality(db: Any, clock: Any) -> None:
    mem = _memory(db, clock)
    _remember(mem, db, "case_a", _alert("a", "a", "5xx:/v1/payments"))
    _remember(mem, db, "case_b", _alert("b", "b", "5xx:/v1/payments"))
    _remember(mem, db, "case_c", _alert("c", "c", "disk:var"))
    with db.connect() as conn:
        similar = sorted(
            conn.execute(text("SELECT src, dst FROM edges WHERE kind = 'similar'")).all()
        )
    assert similar == [
        ("case:acme:case_a", "case:acme:case_b"),
        ("case:acme:case_b", "case:acme:case_a"),
    ]


def test_citation_must_reference_existing_case(
    db: Any, uow: Any, clock: Any, ids: Any, events: Any
) -> None:
    _case(db, "case_1")
    gate = GateService(
        uow=uow,
        clock=clock,
        ids=ids,
        events=events,
        catalog=catalog("payments"),
        ttl=timedelta(hours=1),
    )
    spec = ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(service="payments", system="servicenow", ref="INC1"),
        scope="service:payments:work_note",
        payload={"template_id": "work_note_v1", "text": "x", "similar_case_id": "case_ghost"},
    )
    with pytest.raises(DomainValidationError, match="case_ghost"):
        gate.submit("case_1", spec, TraceContext.new("acme", "tr", "s"))
    ok = spec.model_copy(update={"payload": {**spec.payload, "similar_case_id": "case_1"}})
    gate.submit("case_1", ok, TraceContext.new("acme", "tr", "s"))


def test_memory_tenant_isolated(db: Any, clock: Any) -> None:
    acme, other = _memory(db, clock, "acme"), _memory(db, clock, "globex")
    _remember(
        other,
        db,
        "case_globex",
        _alert("Payment API 5xx", "5xx", "5xx:/v1/payments", tenant="globex"),
    )
    _remember(
        acme, db, "case_acme", _alert("Payment API 5xx", "5xx", "5xx:/v1/payments"), resolved=False
    )
    assert acme.similar("case_acme") == [], "another tenant's case is never cited"
    with db.connect() as conn:
        edges = conn.execute(text("SELECT COUNT(*) FROM edges WHERE kind = 'similar'"))
        assert edges.scalar_one() == 0, "no similar edge crosses tenants"


def test_memory_text_is_redacted(db: Any, clock: Any) -> None:
    mem = _memory(db, clock)
    _remember(
        mem,
        db,
        "case_p",
        _alert("Card 4242424242424242 declined", "caller support+ticket@acme-corp.example", None),
    )
    with db.connect() as conn:
        stored = conn.execute(text("SELECT text FROM kb_docs")).scalar_one()
    assert "4242424242424242" not in stored and "acme-corp.example" not in stored


def test_bm25_prefers_rarer_shared_terms() -> None:
    docs = [
        MemoryDoc("a", "timeout timeout gateway", None, None, None, None, 1.0),
        MemoryDoc("b", "disk full", None, None, None, None, 1.0),
    ]
    scores = bm25_scores("gateway timeout", docs)
    assert scores["a"] > 0 and scores["b"] == 0
    query = MemoryDoc("q", "gateway timeout", None, None, None, None, 0.0)
    assert [c.case_id for c in rank(query, docs)] == ["a"]


def test_triage_cites_the_similar_past_case(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    from tests.unit.test_triage_service import _seed_grant  # noqa: PLC0415

    fake_llm.respond(_llm_json())
    mem = CaseMemory(
        uow=uow, clock=clock, tenant_id="acme", redact=lambda t: _REDACTOR.redact(t).text
    )
    w = _wire(uow, clock, ids, aead, fake_llm, memory=mem)
    first = w.triage.handle(_evt(error_signature="5xx:/v1/payments"))
    with db.begin() as conn:
        conn.execute(
            text("UPDATE cases SET status = 'resolved' WHERE id = :c"), {"c": first.case_id}
        )
    mem.index_resolved(first.case_id)
    _seed_grant(db)
    second = w.triage.handle(
        _evt(
            error_signature="5xx:/v1/payments",
            resource="payments-api",
            subject="Payment API 5xx again",
        )
    )
    assert isinstance(second, Triaged)
    (note,) = second.proposals
    assert note.payload["similar_case_id"] == first.case_id
    assert f"similar past case {first.case_id}" in str(note.payload["text"])
