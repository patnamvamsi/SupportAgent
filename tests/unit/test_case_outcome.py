"""ADR-0024: an alerts-only system learns from outcomes a person records on the case.

A case with no ticket is resolved on its page, with what it really was. That is a
calibration observation of triage's label and, with no incident history, a member
of its label's readiness class, under the history classes' floors and gates.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.live_proof import LiveProof
from supportagent.domain.model.readiness import ReadinessGates
from supportagent.domain.outcomes import AlertOutcome
from supportagent.evals.readiness import (
    alert_observations,
    assess_alert_outcomes,
    fit_calibration,
    redecide,
)
from tests.unit.api_support import ADA, BOB, TENANT, login, make_client, seed_users

pytestmark = pytest.mark.unit

_LABELS = ["runtime/down", "runtime/stalled"]
_RCA = {
    "outcome": "SUPPORTED",
    "supported_by": "container-down",
    "hypotheses": ["compose_host_restart", "compose_container_down"],
    "evidence": [
        {"step": "host-restart", "hypothesis": "compose_host_restart", "ok": False, "ran": True},
        {"step": "container-down", "hypothesis": "compose_container_down", "ok": True, "ran": True},
    ],
    "claims": [],
    "plan": "runbook:runtime-down",
    "reason": "container down",
}


def _seed_case(db: Any, case_id: str, *, ticket: str | None = None, rca: bool = True) -> None:
    decision = {
        "outcome": "safe_default",
        "reason": "low-calibrated-score",
        "classification": "runtime/down",
        "severity": "P1",
        "preclass_label": "runtime/down",
    }
    with db.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO cases (id, tenant_id, fingerprint, origin, status, trace_id, "
                "duplicate_count, opened_ts, last_alert_ts, ticket_number, service, severity, "
                "classification, decision_json) VALUES (:id, :t, :fp, 'alert', 'triaged', :tr, "
                "0, 0, 0, :ticket, 'payments', 'P1', 'runtime/down', :dj)"
            ),
            {
                "id": case_id,
                "t": TENANT,
                "fp": f"fp_{case_id}",
                "tr": f"tr_{case_id}",
                "ticket": ticket,
                "dj": json.dumps(decision),
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO shadow_records (id, tenant_id, case_id, alert_received_ts, "
                "proposed_json) VALUES (:id, :t, :c, 0, :pj)"
            ),
            {
                "id": f"sh_{case_id}",
                "t": TENANT,
                "c": case_id,
                "pj": json.dumps({"decision": decision}),
            },
        )
        if rca:
            conn.execute(
                sa.text(
                    "INSERT INTO rca_artifacts (id, tenant_id, case_id, outcome, artifact_json, "
                    "created_ts) VALUES (:id, :t, :c, 'SUPPORTED', :aj, 0)"
                ),
                {"id": f"rca_{case_id}", "t": TENANT, "c": case_id, "aj": json.dumps(_RCA)},
            )


def _status(db: Any, case_id: str) -> str:
    with db.connect() as conn:
        return str(
            conn.execute(sa.text("SELECT status FROM cases WHERE id = :c"), {"c": case_id}).scalar()
        )


def _client(db: Any, clock: Any, ids: Any, master_key: bytes) -> tuple[Any, FakeOIDC]:
    oidc = FakeOIDC()
    http = make_client(db, clock, ids, master_key, oidc, outcome_labels=lambda _: list(_LABELS))
    return http, oidc


def _post(http: Any, case_id: str, **form: str) -> Any:
    data = {"csrf_token": http.cookies["sa_csrf"], **form}
    return http.post(f"/ui/cases/{case_id}/outcome", data=data, follow_redirects=False)


def test_a_person_resolves_a_ticketless_case_with_its_outcome(
    db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    _seed_case(db, "case_a")
    http, oidc = _client(db, clock, ids, master_key)
    login(http, oidc, ADA)
    page = http.get("/ui/cases/case_a").text
    assert "Resolve with this outcome" in page
    assert "compose_container_down" in page, "the person judges the RCA's cause"

    missing = _post(http, "case_a", classification="runtime/down", severity="P1")
    assert missing.status_code != 303 and "real one" in missing.text
    unknown = _post(http, "case_a", classification="db/slow", severity="P1", cause="right")
    assert unknown.status_code != 303, "a label the service doesn't have"
    assert _status(db, "case_a") == "triaged"

    done = _post(
        http, "case_a", classification="runtime/down", severity="P2", cause="right", drill="on"
    )
    assert done.status_code == 303, done.text[:300]
    assert _status(db, "case_a") == "resolved"
    assert "Resolve with this outcome" not in http.get("/ui/cases/case_a").text
    with db.connect() as conn:
        payload = json.loads(
            conn.execute(
                sa.text(
                    "SELECT payload FROM feedback WHERE case_id = 'case_a' AND kind = 'outcome'"
                )
            ).scalar_one()
        )
        shadow = conn.execute(
            sa.text(
                "SELECT human_classification, human_severity, agreement FROM shadow_records "
                "WHERE case_id = 'case_a'"
            )
        ).one()
    assert payload | {"first_human_action_at": None} == {
        "resolution": "resolved",
        "source": "case",
        "classification": "runtime/down",
        "severity": "P2",
        "cause": "right",
        "drill": True,
        "first_human_action_at": None,
    }
    assert tuple(shadow) == ("runtime/down", "P2", 0), "the label held; the severity didn't"
    again = _post(http, "case_a", classification="runtime/down", severity="P1", cause="right")
    assert again.status_code != 303, "a resolved case isn't resolved twice"

    (seen,) = LiveProof(uow=SqlUnitOfWork(db), tenant_id=TENANT).alert_outcomes()
    assert (seen.label, seen.cause, seen.drill, seen.predicted, seen.preclass) == (
        "runtime/down",
        "right",
        True,
        "runtime/down",
        "runtime/down",
    )
    assert seen.supported == "compose_container_down"


def test_only_a_ticketless_case_and_a_permitted_person(
    db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    _seed_case(db, "case_t", ticket="INC0001")
    _seed_case(db, "case_v")
    seed_users(db, ("sub-ada", "admin"), ("sub-bob", "viewer"))
    http, oidc = _client(db, clock, ids, master_key)
    login(http, oidc, ADA)
    assert "Resolve with this outcome" not in http.get("/ui/cases/case_t").text
    ticketed = _post(http, "case_t", classification="runtime/down", severity="P1", cause="right")
    assert ticketed.status_code != 303 and _status(db, "case_t") == "triaged"

    viewer, oidc_b = _client(db, clock, ids, master_key)
    login(viewer, oidc_b, BOB)
    assert "Resolve with this outcome" not in viewer.get("/ui/cases/case_v").text
    refused = _post(viewer, "case_v", classification="runtime/down", severity="P1", cause="right")
    assert refused.status_code == 403
    assert _status(db, "case_v") == "triaged"


# --- readiness and calibration from recorded outcomes ------------------------------------


def _outcome(n: int, **overrides: Any) -> AlertOutcome:
    fields: dict[str, Any] = {
        "case_id": f"case_{n:02d}",
        "label": "runtime/down",
        "severity": "P1",
        "cause": "right",
        "drill": False,
        "predicted": "runtime/down",
        "preclass": "runtime/down",
        "outcome": "safe_default",
        "reason": "low-calibrated-score",
        "triage_severity": "P1",
        "verdict": "SUPPORTED",
        "supported": "compose_container_down",
    }
    return AlertOutcome(**(fields | overrides))


def _assess(outcomes: list[AlertOutcome]) -> Any:
    table = fit_calibration([], model_id="m", system="s", observed=alert_observations(outcomes))
    (cls,) = assess_alert_outcomes(outcomes, ReadinessGates(), calibration=table, min_score=0.8)
    return table, cls


def test_a_label_routes_once_enough_outcomes_confirm_it() -> None:
    """The floor is ADR-0002's: 16 correct with the pre-classifier agreeing clear 0.80."""
    table, cls = _assess([_outcome(n) for n in range(15)])
    row = table.labels["runtime/down"].agree
    assert row is not None and (row.n, row.correct) == (15, 15)
    assert cls.status == "not_ready"
    assert any("needs 1 more correctly classified" in r for r in cls.reasons), cls.reasons
    assert cls.correct == 15 and cls.hypothesis == "compose_container_down"

    _, ready = _assess([_outcome(n) for n in range(16)])
    assert ready.status == "ready", ready.reasons
    assert ready.routed == 16 and ready.share == 1.0


def test_a_cause_the_person_rejected_is_a_false_cause() -> None:
    outcomes = [_outcome(n) for n in range(16)] + [_outcome(16, cause="wrong")]
    _, cls = _assess(outcomes)
    assert cls.status == "not_ready"
    assert cls.false_causes == (("case_16", "compose_container_down"),)


def test_redecide_keeps_triage_order_and_the_severity_cap() -> None:
    _, cls = _assess([_outcome(n) for n in range(16)])
    table = fit_calibration(
        [], model_id="m", system="s", observed=alert_observations([_outcome(n) for n in range(16)])
    )
    unlabelled = redecide(_outcome(0, preclass=None), table, 0.8)
    assert unlabelled.reason == "low-calibrated-score", "its abstain row has no evidence"
    capped = fit_calibration(
        [],
        model_id="m",
        system="s",
        observed=alert_observations([_outcome(n, preclass=None) for n in range(16)]),
    )
    assert redecide(_outcome(0, preclass=None), capped, 0.8).reason == "unconfirmed-high-severity"
    minor = _outcome(0, preclass=None, triage_severity="P3")
    assert redecide(minor, capped, 0.8).outcome == "routed"
    assert redecide(_outcome(0, preclass="db/slow"), table, 0.8).reason == "classifier-conflict"
    assert redecide(_outcome(0, reason="storm-member"), table, 0.8).reason == "storm-member"
    assert cls.status == "ready"
