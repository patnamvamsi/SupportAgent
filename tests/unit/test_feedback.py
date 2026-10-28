"""WP-109: feedback capture, the three moments (ARCH §8.3).

Canonical test IDs from 11-wp §WP-109 that live here:
``test_feedback_approval_reason_captured``, ``test_feedback_diff_from_human_change``.
The resolve-time ones run through the worker in ``tests/integration/test_wp109_feedback.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.fakes.oidc import FakeOIDC
from supportagent.app.feedback import FeedbackService
from supportagent.domain.model.ticket import ChangeRecord
from tests.unit.api_support import (
    ADA,
    BOB,
    TENANT,
    csrf,
    login,
    logout,
    make_client,
    seed_proposal,
    seed_users,
)

_T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _rows(db: Any, kind: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT payload FROM feedback WHERE kind = :k ORDER BY ts, id"), {"k": kind}
        ).scalars()
        return [json.loads(r) for r in rows]


def test_feedback_approval_reason_captured(
    db: Any, clock: Any, ids: Any, master_key: bytes
) -> None:
    oidc = FakeOIDC()
    client = make_client(db, clock, ids, master_key, oidc)
    ok = seed_proposal(db, proposal_id="prop_ok", case_id="case_a")
    no = seed_proposal(db, proposal_id="prop_no", case_id="case_b", external_id="INC2")
    login(client, oidc, ADA)
    logout(client)
    seed_users(db, ("sub-bob", "approver"))
    login(client, oidc, BOB)
    assert (
        client.post(
            "/api/v1/proposals/prop_ok/approve",
            json={"payload_hash": ok, "reason": "matches the runbook"},
            headers=csrf(client),
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/proposals/prop_no/reject",
            json={"payload_hash": no, "reason": "wrong service, it's the LB"},
            headers=csrf(client),
        ).status_code
        == 200
    )
    rows = _rows(db, "approval")
    assert [(r["proposal_id"], r["decision"], r["reason"]) for r in rows] == [
        ("prop_ok", "approved", "matches the runbook"),
        ("prop_no", "rejected", "wrong service, it's the LB"),
    ]
    with db.connect() as conn:
        events = conn.execute(text("SELECT actor FROM events WHERE kind = 'feedback'")).all()
    assert [e[0] for e in events] == ["sub-bob", "sub-bob"], "feedback is on the trace too"


def _change(user: str, field: str, value: str, minute: int) -> ChangeRecord:
    return ChangeRecord(
        source="audit",
        user=user,
        ts=_T0 + timedelta(minutes=minute),
        fieldname=field,
        newvalue=value,
        checkpoint=minute + 2,
    )


def test_feedback_diff_from_human_change(db: Any, clock: Any, ids: Any, events: Any) -> None:
    seed_proposal(db, proposal_id="prop_wn", case_id="case_1")
    with db.begin() as conn:
        conn.execute(
            text("UPDATE cases SET decision_json = :d WHERE id = 'case_1'"),
            {"d": json.dumps({"severity": "P3", "classification": "payments/api"})},
        )
        conn.execute(
            text(
                "INSERT INTO proposals (id, tenant_id, case_id, action_kind, scope, payload_json, "
                "payload_hash, trace_id, status, created_by, created_at_ts) VALUES ('prop_tu', "
                "'acme', 'case_1', 'ticket_update', 's', :p, 'h', 'tr', 'pending_approval', "
                "'agent', 2)"
            ),
            {"p": json.dumps({"fields": {"urgency": "2"}})},
        )
    service = FeedbackService(
        uow=SqlUnitOfWork(db),
        clock=clock,
        ids=ids,
        events=events,
        tenant_id=TENANT,
        integration_user="svc_supportagent",
    )
    written = service.record_changes(
        "case_1",
        [
            _change("bob", "priority", "2", 1),  # vs the decision's P3: contradicted
            _change("bob", "urgency", "2", 2),  # vs the ticket_update proposal: confirmed
            _change("carol", "assigned_to", "u-carol", 3),  # a human action, nothing to compare
            _change("svc_supportagent", "priority", "3", 4),  # our own write: not feedback
            _change("bob", "short_description", "x", 5),  # not tracked, not proposed
        ],
    )
    assert written == 3
    diffs = _rows(db, "diff")
    assert [(d["field"], d.get("paired_with"), d["verdict"]) for d in diffs] == [
        ("priority", "decision", "contradicted"),
        ("urgency", "prop_tu", "confirmed"),
        ("assigned_to", None, "human_action"),
    ]
