"""WP-001: domain models (01 §4) — shapes and domain rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.events import EventKind
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model import (
    ActionSpec,
    AlertEvent,
    Approval,
    CommsPlan,
    Event,
    Grant,
    Proposal,
    TargetRef,
    TriageDecision,
    TriageResult,
    User,
)
from supportagent.domain.model.approval import ApprovalStatus
from supportagent.domain.model.comms import CommsPlanStatus
from supportagent.domain.model.grant import GrantStatus
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.severity import Severity
from tests.builders import T0, a_approval, a_grant, a_proposal, a_ticket, an_alert

UTC_PLUS_5 = timezone(timedelta(hours=5))


def test_alert_event_rejects_naive_datetime() -> None:
    # 01 §4: "All datetimes are timezone-aware UTC; a naive datetime is a
    # validation error" — pydantic strict passes naive datetimes through, so
    # the rule must be an explicit validator.
    with pytest.raises(ValidationError, match="aware UTC"):
        # Deliberately naive: the rejection IS the test.
        AlertEvent(**an_alert(received_at=datetime(2026, 10, 20, 9, 0, 0)))  # noqa: DTZ001
    # Aware UTC is accepted.
    event = AlertEvent(**an_alert())
    assert event.received_at == T0
    assert event.received_at.utcoffset() == timedelta(0)


def test_alert_event_rejects_non_utc_datetime() -> None:
    with pytest.raises(ValidationError, match="aware UTC"):
        AlertEvent(**an_alert(received_at=datetime(2026, 10, 20, 14, 0, 0, tzinfo=UTC_PLUS_5)))


def test_alert_event_ticket_requires_number() -> None:
    # origin == "ticket" without ticket_number is a domain rule violation (01 §4).
    with pytest.raises(ValidationError, match="ticket_number"):
        AlertEvent(**a_ticket(ticket_number=None))
    ticket = AlertEvent(**a_ticket())
    assert ticket.origin == "ticket"
    assert ticket.ticket_number == "INC-2026-42"
    # Alerts do not carry a ticket number.
    assert AlertEvent(**an_alert(ticket_number=None)).ticket_number is None
    # Unknown fields are forbidden (strict models).
    with pytest.raises(ValidationError):
        AlertEvent(**an_alert(not_a_field=1))


def _target(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "service": "payments-api",
        "system": "servicenow",
        "ref": "incident:INC0000001",
    }
    base.update(overrides)
    return base


def test_action_spec_rejects_float_payload() -> None:
    # 01 §4: ActionSpec.payload — "floats forbidden (§7.1)".
    with pytest.raises(DomainValidationError, match="floats are not allowed in hashed payloads"):
        ActionSpec(
            kind=ActionKind.WORK_NOTE,
            target=TargetRef(**_target()),
            scope="ticket:INC0000001",
            payload={"lat": 40.7},
        )
    spec = ActionSpec(
        kind=ActionKind.WORK_NOTE,
        target=TargetRef(**_target()),
        scope="ticket:INC0000001",
        payload={"lat": 40, "lon": 7, "nested": {"a": [1, 2]}},
    )
    assert spec.kind is ActionKind.WORK_NOTE
    assert spec.payload == {"lat": 40, "lon": 7, "nested": {"a": [1, 2]}}
    # Scope is a literal, non-empty string (ARCH §6.4).
    with pytest.raises(ValidationError):
        ActionSpec(kind=ActionKind.WORK_NOTE, target=TargetRef(**_target()), scope="", payload={})


def test_event_payload_requires_version() -> None:
    # 01 §4: Event.payload "must contain 'v': <int> payload schema version".
    def event_kwargs(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "id": "evt_0001",
            "tenant_id": "acme",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": None,
            "kind": EventKind.INGEST,
            "actor": "agent",
            "payload": {"v": 1, "external_id": "INC0000001"},
            "ts": T0,
        }
        base.update(overrides)
        return base

    with pytest.raises(ValidationError, match="v"):
        Event(**event_kwargs(payload={"external_id": "INC0000001"}))
    # "v" must be an int (bool is an int subclass — reject it).
    with pytest.raises(ValidationError, match="v"):
        Event(**event_kwargs(payload={"v": True}))
    event = Event(**event_kwargs())
    assert event.payload["v"] == 1
    assert event.kind is EventKind.INGEST
    assert event.tokens_in == 0 and event.status == "ok"


def test_triage_result_and_decision_shapes() -> None:
    result = TriageResult(
        classification="payments/timeout",
        severity=Severity.P1,
        summary="5xx spike on /v1/payments after the 08:35 deploy.",
        confidence=0.91,
    )
    assert result.classification == "payments/timeout"
    # Classification is a two-part lowercase slug (01 §4 pattern).
    with pytest.raises(ValidationError):
        TriageResult(
            classification="Bad Classification",
            severity=Severity.P1,
            summary="x",
            confidence=0.5,
        )
    decision = TriageDecision(
        outcome="safe_default",
        reason="llm-failure",
        classification=None,
        severity=None,
        preclass_label=None,
        calibrated_score=None,
        route_id=None,
        llm_suggestion=None,
    )
    assert decision.outcome == "safe_default"
    assert decision.llm_suggestion is None


def test_proposal_shape() -> None:
    proposal = Proposal(**a_proposal())
    assert proposal.status is ProposalStatus.PROPOSED
    assert proposal.kind is ActionKind.WORK_NOTE
    assert proposal.payload_hash == "a" * 64
    assert proposal.created_by == "agent"


def test_approval_shape() -> None:
    approval = Approval(**a_approval())
    assert approval.status is ApprovalStatus.APPROVED
    assert approval.reason is None
    # The hash the approver actually saw (ARCH §6.2).
    assert approval.payload_hash == "a" * 64


def test_grant_shape() -> None:
    grant = Grant(**a_grant())
    assert grant.status is GrantStatus.APPROVED
    assert grant.granted_by == "user_bo"
    # A requested grant is not enforceable yet: approval fields are NULL (ARCH §6.2).
    requested = Grant(
        **a_grant(status=GrantStatus.REQUESTED, granted_by=None, granted_at=None, valid_to=None)
    )
    assert requested.granted_by is None
    assert requested.granted_at is None
    assert requested.valid_to is None


def test_comms_plan_shape() -> None:
    plan = CommsPlan(
        id="plan_0001",
        tenant_id="acme",
        case_id="case_0001",
        audience="customer",
        channel="email",
        template_id="tpl_incident_update",
        allowed_fields={"state": "record_state", "next_update_eta": "schedule"},
        cadence_minutes=15,
        plan_hash="b" * 64,
        proposal_id="prop_0001",
        approval_id=None,
        status=CommsPlanStatus.PENDING_APPROVAL,
    )
    assert plan.audience == "customer"
    assert plan.cadence_minutes == 15
    assert plan.status is CommsPlanStatus.PENDING_APPROVAL
    assert plan.approval_id is None


def test_user_shape() -> None:
    user = User(
        id="user_ada",
        tenant_id="acme",
        name="Ada",
        email="ada@example.com",
        is_active=True,
    )
    # 01 §3/ARCH §6.4: users.id is the stable OIDC sub.
    assert user.id == "user_ada"
    assert user.is_active is True
