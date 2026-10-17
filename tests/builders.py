"""Shared test builders (04 §3): tests declare only the fields that matter.

``an_alert`` / ``a_ticket`` / ``a_proposal`` / ``an_approval`` / ``a_grant``
land with the WP that owns the models (WP-001); the remaining builders land
with WP-004. Builders return ``**kwargs`` dicts so a test overrides exactly
one field at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.approval import ApprovalStatus
from supportagent.domain.model.grant import GrantStatus
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.severity import Severity

# 04 §3 canonical frozen-clock start.
T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)


def an_alert(**overrides: object) -> dict[str, object]:
    """A valid AlertEvent (01 §4) with one field overridable at a time."""
    base: dict[str, object] = {
        "tenant_id": "acme",
        "source": "servicenow",
        "origin": "alert",
        "external_id": "INC0000001",
        "table": "incident",
        "ticket_number": None,
        "received_at": T0,
        "subject": "Payment API 5xx spike",
        "body": "5xx errors on /v1/payments since 08:40 UTC.",
        "resource": "payments-api",
        "alert_type": "error_rate",
        "error_signature": "5xx:/v1/payments",
        "severity_hint": Severity.P1,
        "cmdb_ci": None,
        "raw_ref": "alerts.payload_ref:acme:INC0000001",
    }
    base.update(overrides)
    return base


def a_ticket(**overrides: object) -> dict[str, object]:
    """A human-filed ticket: origin == 'ticket' with a ticket_number."""
    return an_alert(
        origin="ticket",
        ticket_number="INC-2026-42",
        subject="Customer reports failed payment",
        body="Customer ticket: payment failed twice.",
        **overrides,
    )


def a_proposal(**overrides: object) -> dict[str, object]:
    """A valid Proposal (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, object] = {
        "id": "prop_0001",
        "tenant_id": "acme",
        "case_id": "case_0001",
        "kind": ActionKind.WORK_NOTE,
        "scope": "ticket:INC0000001",
        "payload": {"body": "Investigated: 5xx spike traced to the 08:35 deploy."},
        "payload_hash": "a" * 64,
        "trace_id": "trace-1",
        "status": ProposalStatus.PROPOSED,
        "created_by": "agent",
        "created_at": T0,
    }
    base.update(overrides)
    return base


def an_approval(**overrides: object) -> dict[str, object]:
    """A valid Approval (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, object] = {
        "id": "appr_0001",
        "tenant_id": "acme",
        "proposal_id": "prop_0001",
        "approver_id": "user_ada",
        "status": ApprovalStatus.APPROVED,
        "payload_hash": "a" * 64,
        "reason": None,
        "approved_at": T0,
    }
    base.update(overrides)
    return base


def a_grant(**overrides: object) -> dict[str, object]:
    """A valid policy grant (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, object] = {
        "id": "grnt_0001",
        "tenant_id": "acme",
        "kind": ActionKind.WORK_NOTE,
        "scope": "ticket:INC0000001",
        "status": GrantStatus.APPROVED,
        "requested_by": "user_ada",
        "requested_at": T0,
        "granted_by": "user_bo",
        "granted_at": T0,
        "valid_to": T0,
    }
    base.update(overrides)
    return base
