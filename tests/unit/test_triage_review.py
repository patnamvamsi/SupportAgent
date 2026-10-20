"""WP-011 review regressions for ``TriageService`` (PR #13 review).

- An LLM-failure safe default still writes the case, the decision and the
  events (10-wp WP-011 "A safe default still writes the case…"; INV-05).
- The ``dedup`` event's ``first_seen`` is True for a new case only.
- Proposals are submitted in the same UoW as the case (10-wp WP-011): a gate
  failure leaves no triaged case behind.
- A severity re-triage never takes a case back from a human (01 §7.11).
- ``redaction.tenant_constants`` fold into the fingerprint (02 §1, 01 §7.2).
- Bootstrap loads the shipped pre-classifier rules (ARCH §4.2).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from supportagent import bootstrap
from supportagent.adapters.fakes.llm import FakeLLMProvider
from supportagent.adapters.llm.faults import FaultInjectingLLMProvider
from supportagent.app.replay import ReplayService
from supportagent.app.triage import TriageService
from supportagent.domain.events import EventKind
from supportagent.domain.model.event import Event
from supportagent.domain.model.triage import Linked, Retriaged, SafeDefaulted, Triaged
from supportagent.domain.severity import Severity
from tests.unit.test_triage_service import _evt, _llm_json, _shadow_config, _wire


class _SpySink:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.appended: list[Event] = []

    def append(self, event: Event) -> None:
        self.appended.append(event)
        self.inner.append(event)


class _FailingGate:
    def submit(self, *_: object) -> Any:
        raise RuntimeError("gate down")  # noqa: TRY003


def _rewire(w: Any, **overrides: Any) -> TriageService:
    parts: dict[str, Any] = {
        "uow": w.uow,
        "clock": w.triage._clock,
        "ids": w.triage._ids,
        "events": w.events,
        "case_repo": w.case_repo,
        "classifier": w.triage._classifier,
        "gate": w.gate,
        "catalog": w.catalog,
    }
    parts.update(overrides)
    return TriageService(**parts)


def _case_rows(db: Any) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT * FROM cases")).mappings()]


def test_llm_failure_safe_default_persists_case_decision_and_events(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPPORTAGENT_FAULT_INJECT", "llm_timeout")
    w = _wire(uow, clock, ids, aead, FaultInjectingLLMProvider(FakeLLMProvider()))
    outcome = w.triage.handle(_evt())
    assert isinstance(outcome, SafeDefaulted)
    rows = _case_rows(db)
    assert len(rows) == 1, "a safe default must still write the case (human queue)"
    assert rows[0]["status"] == "triaged"
    assert json.loads(rows[0]["decision_json"])["reason"] == "llm-failure"
    with w.uow:
        kinds = [e.kind for e in ReplayService(repo=w.events).replay(rows[0]["trace_id"]).events]
    assert EventKind.DECISION in kinds
    assert EventKind.LLM in kinds


def test_dedup_event_first_seen_true_only_for_new_case(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    spy = _SpySink(w.events)
    triage = _rewire(w, events=spy)
    assert isinstance(triage.handle(_evt()), Triaged)
    assert isinstance(triage.handle(_evt()), Linked)
    dedups = [e.payload["first_seen"] for e in spy.appended if e.kind == EventKind.DEDUP]
    assert dedups == [True, False]


def test_triage_gate_failure_leaves_no_case(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    triage = _rewire(w, gate=_FailingGate())
    with pytest.raises(RuntimeError, match="gate down"):
        triage.handle(_evt())
    assert _case_rows(db) == [], "case + proposals are one UoW: no routed case without proposals"


def test_escalation_does_not_take_case_from_human(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    first = w.triage.handle(_evt(severity_hint=Severity.P3))
    assert isinstance(first, Triaged)
    with db.begin() as conn:
        conn.execute(text("UPDATE cases SET status = 'human_owned'"))
    second = w.triage.handle(_evt(severity_hint=Severity.P1))
    assert isinstance(second, Retriaged)
    assert _case_rows(db)[0]["status"] == "human_owned"


def test_tenant_constants_fold_into_fingerprint(  # noqa: PLR0913, PLR0917
    db: Any, uow: Any, clock: Any, ids: Any, aead: Any, fake_llm: Any
) -> None:
    fake_llm.respond(_llm_json())
    w = _wire(uow, clock, ids, aead, fake_llm)
    triage = _rewire(w, tenant_constants=["eu-west-1", "us-east-1"])
    first = triage.handle(_evt(error_signature="timeout in eu-west-1"))
    second = triage.handle(_evt(error_signature="timeout in us-east-1"))
    assert isinstance(first, Triaged)
    assert isinstance(second, Linked)


def test_bootstrap_loads_shipped_preclass_rules() -> None:
    rule_set = bootstrap._build_ruleset(_shadow_config())
    assert rule_set.rules, "the container must not run with an empty pre-classifier"
