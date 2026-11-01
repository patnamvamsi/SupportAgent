"""WP-O6b (ADR-0015 §5): routing follows readiness.

Triage safe-defaults a label no ready class carries; RCA never lets a claim
for an unproven cause be sent. A system without a report routes as before.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.rca import Budget
from supportagent.app.readiness_store import ReadinessStore
from supportagent.domain.model.triage import TriageResult
from supportagent.domain.readiness import NOT_READY_TAG, ReadinessPolicy
from supportagent.domain.severity import Severity
from supportagent.domain.slots import SlotName
from tests.unit.api_support import ADA, TENANT, login, make_client
from tests.unit.test_classify import _LABEL, TRACE, _case, _classifier, _evt
from tests.unit.test_rca import (
    FAILED_RUN,
    POOL_LOGS,
    ROOT,
    FakeTools,
    ScriptedLLM,
    _knowledge,
    _service,
    _supports,
)
from tests.unit.test_rca import _case as _rca_case
from tests.unit.test_readiness import _report

pytestmark = pytest.mark.unit

_CLASSES: list[dict[str, Any]] = [
    {"label": _LABEL, "hypothesis": "oracle_lock_contention", "status": "ready"},
    {"label": _LABEL, "hypothesis": "jvm_connection_pool_exhaustion", "status": "not_ready"},
    {"label": "app/config", "hypothesis": "ui_cors_or_stale_bundle", "status": "untestable"},
    {"label": "request/access", "hypothesis": None, "status": "ready"},
]


def test_the_policy_routes_ready_labels_exactly_and_names_unproven_causes() -> None:
    policy = ReadinessPolicy.of(_CLASSES)
    assert policy.routes(_LABEL) and policy.routes("request/access")
    assert not policy.routes("app/config") and not policy.routes(None)
    assert not policy.routes(_LABEL.split("/")[0] + "/other")  # same domain isn't enough
    assert policy.proven("oracle_lock_contention")
    assert not policy.proven("jvm_connection_pool_exhaustion")


def _decide(policy: ReadinessPolicy | None, label: str) -> Any:
    llm = TriageResult(classification=label, severity=Severity.P3, summary="s", confidence=0.9)
    classifier = _classifier(llm_result=llm)
    classifier.readiness = policy
    return classifier.decide(_evt(severity_hint=Severity.P3), _case(), TRACE)


def test_triage_routes_a_ready_label_and_safe_defaults_the_rest() -> None:
    assert _decide(None, _LABEL).outcome == "routed"  # no report: unchanged
    assert _decide(ReadinessPolicy.of(_CLASSES), _LABEL).outcome == "routed"
    unproven = ReadinessPolicy.of([c for c in _CLASSES if c["status"] != "ready"])
    held = _decide(unproven, _LABEL)
    assert (held.outcome, held.reason) == ("safe_default", "class-not-ready")
    assert held.llm_suggestion is not None and held.classification == _LABEL


def test_rca_never_lets_a_claim_for_an_unproven_cause_be_sent() -> None:
    def run(policy: ReadinessPolicy | None) -> Any:
        tools = FakeTools({"gha.recent_runs": FAILED_RUN, "logs.grep": POOL_LOGS})
        service = _service(
            _knowledge(ROOT / "runbooks"), tools, ScriptedLLM({SlotName.ENTAIL: _supports})
        )
        service._readiness = policy
        return service.run(_rca_case(), Budget())

    proven = run(None)
    assert proven.outcome == "SUPPORTED" and all(c.sendable for c in proven.claims)
    unproven = run(ReadinessPolicy(frozenset(), frozenset({"recent_change_push"})))
    assert unproven.outcome == "SUPPORTED"  # the evidence still says so, for a person
    assert unproven.claims and all(NOT_READY_TAG in c.tags for c in unproven.claims)
    assert not any(c.sendable for c in unproven.claims) and unproven.sop_steps == []


def test_the_store_keeps_the_last_report_per_tenant(db: Any, clock: Any) -> None:
    store = ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    assert store.load() is None and store.policy() is None
    store.save({"system": "a", "classes": _CLASSES[:1]})
    store.save({"system": "a", "classes": _CLASSES})
    loaded = store.load()
    assert loaded is not None and len(loaded["classes"]) == 4
    policy = store.policy()
    assert policy is not None and policy.routes("request/access")
    other = ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id="other-tenant")
    assert other.load() is None


@pytest.fixture
def oidc() -> FakeOIDC:
    return FakeOIDC()


@pytest.fixture
def client(db: Any, clock: Any, ids: Any, master_key: bytes, oidc: FakeOIDC) -> TestClient:
    return make_client(db, clock, ids, master_key, oidc)


def test_the_readiness_page_shows_each_class_and_why(
    client: TestClient, oidc: FakeOIDC, db: Any, clock: Any
) -> None:
    login(client, oidc, ADA)
    assert "No readiness report yet" in client.get("/ui/onboarding/readiness").text
    report = _report("system-a").as_dict()
    ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT).save(report)
    assert "% of incident weight proven" in client.get("/ui/onboarding").text
    page = client.get("/ui/onboarding/readiness").text
    assert "Ready to support" in page and "<meter" in page and "style=" not in page
    assert "Can&#39;t be tested yet" in page and "angular plugin is a skeleton" in page
    assert "<strong>3 of 6</strong>" in page and "below the 75% needed" in page  # held back


def _replay(predicted: str, preclass: str | None, *, ok: bool) -> Any:
    from supportagent.evals.lab import LabScore  # noqa: PLC0415
    from supportagent.evals.readiness import Replayed  # noqa: PLC0415

    score = LabScore("s", "tune", ok, True, True, True, False, False, True, None, 1, 0, ())
    return Replayed("I", None, score, predicted=predicted, preclass=preclass)


def test_calibration_is_fitted_from_the_replayed_history() -> None:
    from supportagent.evals.readiness import fit_calibration  # noqa: PLC0415

    table = fit_calibration(
        [
            _replay("database/saturation", None, ok=True),
            _replay("database/saturation", None, ok=False),
            _replay("database/saturation", "database/latency", ok=True),  # agrees (same domain)
            _replay("app/memory", "network/outage", ok=True),  # a conflict: not evidence
        ],
        model_id="m",
        system="s",
    )
    row = table.labels["database/saturation"]
    assert row.abstain is not None and (row.abstain.n, row.abstain.correct) == (2, 1)
    assert row.agree is not None and (row.agree.n, row.agree.correct) == (1, 1)
    assert "app/memory" not in table.labels
    assert table.grade == "holdout" and table.signature == table.computed_signature()


def test_triage_uses_the_fitted_calibration_only_for_its_own_model(db: Any, clock: Any) -> None:
    from supportagent.bootstrap import _fitted_calibration  # noqa: PLC0415
    from supportagent.evals.readiness import fit_calibration  # noqa: PLC0415
    from tests.unit.test_triage_service import _shadow_config  # noqa: PLC0415

    config = _shadow_config()
    config = config.model_copy(update={"tenant": config.tenant.model_copy(update={"id": TENANT})})
    model = config.llm.slots["classify"].model_id
    store = ReadinessStore(uow=SqlUnitOfWork(db), clock=clock, tenant_id=TENANT)
    uow = SqlUnitOfWork(db)
    assert _fitted_calibration(config, uow, clock) is None  # never assessed: the artifact
    fitted = fit_calibration([_replay("app/memory", None, ok=True)], model_id=model, system="s")
    store.save({"classes": [], "calibration": fitted.model_dump(mode="json")})
    got = _fitted_calibration(config, uow, clock)
    assert got is not None and got.labels["app/memory"].abstain is not None
    other = fit_calibration([_replay("app/memory", None, ok=True)], model_id="other", system="s")
    store.save({"classes": [], "calibration": other.model_dump(mode="json")})
    assert _fitted_calibration(config, uow, clock) is None  # fitted for another model: stale
    tampered = fitted.model_dump(mode="json")
    tampered["labels"]["app/memory"]["abstain"]["n"] = 100
    tampered["labels"]["app/memory"]["abstain"]["correct"] = 100
    store.save({"classes": [], "calibration": tampered})
    assert _fitted_calibration(config, uow, clock) is None  # its counts were edited
