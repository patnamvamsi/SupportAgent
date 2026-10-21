"""Shared test builders (04 §3): tests declare only the fields that matter.

``an_alert`` / ``a_ticket`` / ``a_proposal`` / ``an_approval`` / ``a_grant``
land with the WP that owns the models (WP-001); the remaining builders land
with WP-004. Builders return ``**kwargs`` dicts so a test overrides exactly
one field at a time.

WP-002 adds file-shaped builders (``a_config_file`` / ``a_catalog_file`` /
``a_classification_file`` / ``a_hypotheses_file``) plus ``SECRET_ENV`` for the
config-loader tests. (``write_yaml_file`` lives in ``tests.yaml_io`` so the red
commit stays pyyaml-free.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.approval import ApprovalStatus
from supportagent.domain.model.grant import GrantStatus
from supportagent.domain.proposal_state import ProposalStatus
from supportagent.domain.severity import Severity

# 04 §3 canonical frozen-clock start.
T0 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)

# 02 §1: secrets live in env vars; the file only names the variable.
# Fake values for tests — the names match the keys of the a_config_file() builder.
SECRET_ENV: dict[str, str] = {
    "SUPPORTAGENT_DB_URL": "postgresql+psycopg://sa:sa@localhost/supportagent",
    "SN_CLIENT_ID": "sn-client-id",
    "SN_CLIENT_SECRET": "sn-client-secret",
    "OIDC_CLIENT_ID": "oidc-client-id",
    "OIDC_CLIENT_SECRET": "oidc-client-secret",
    # >= 32 bytes per 02 §1 (identity.session_secret). Low-entropy by design:
    # this is a fake test value, and a high-entropy literal here trips the
    # gitleaks generic-api-key rule in CI.
    "SA_SESSION_SECRET": "test" * 8,
    "ANTHROPIC_API_KEY": "sk-ant-dummy-test-key",
    "PAGERDUTY_TOKEN": "pd-dummy-test-token",
}


def a_config_file(**overrides: object) -> dict[str, Any]:
    """A valid supportagent.yaml v1 file (02 §1) with every key present."""
    base: dict[str, Any] = {
        "config_version": 1,
        "tenant": {"id": "acme", "display_name": "Acme Corp"},
        "mode": "shadow",
        "database": {"url_env": "SUPPORTAGENT_DB_URL"},
        "servicenow": {
            "instance_url": "https://acme.service-now.com",
            "auth": {
                "kind": "oauth_client_credentials",
                "client_id_env": "SN_CLIENT_ID",
                "client_secret_env": "SN_CLIENT_SECRET",
            },
            "integration_user": "svc_supportagent",
            "tables": ["incident"],
            "poll_interval_s": 60,
            "overlap_s": 120,
            "page_size": 200,
            "qps_limit": 5,
        },
        "identity": {
            "oidc_issuer": "https://login.microsoftonline.com/acme-tenant-id/v2.0",
            "client_id_env": "OIDC_CLIENT_ID",
            "client_secret_env": "OIDC_CLIENT_SECRET",
            "redirect_url": "https://supportagent.acme.internal/auth/callback",
            "session_secret_env": "SA_SESSION_SECRET",
        },
        "llm": {
            "provider": "anthropic",
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": None,
            "slots": {
                "classify": {
                    "model_id": "claude-sonnet-4-5-20250929",  # dated snapshot (02 §1)
                    "timeout_s": 15,
                    "max_output_tokens": 400,
                }
            },
            "daily_budget_tokens": 2_000_000,
        },
        "redaction": {"custom_patterns": [], "tenant_constants": []},
        "calibration": {
            "path": "evals/artifacts/calibration.json",
            "min_grade": "smoke",
            "min_score": 0.80,
        },
        "paths": {
            "taxonomy": "taxonomy/classification.yaml",
            "hypotheses": "taxonomy/hypotheses.yaml",
            "catalog": "catalog/services.yaml",
            "prompts": "prompts/",
            "runbooks": "runbooks/",
        },
        "routing": [
            {
                "id": "db-saturation",
                "match": {"classification": "database/saturation", "severity": ["P1", "P2", "P3"]},
                "actions": ["work_note"],
                "rca": "none",
                "comms": None,
            }
        ],
        "default": {"id": "generic", "actions": ["work_note"], "rca": "none"},
        "proposals": {"ttl_hours": 24},
        "grants_request": [],
        "retention": {"events_days": 395, "comms_days": 395, "alerts_days": 90},
    }
    base.update(overrides)
    return base


def a_catalog_file(**overrides: object) -> dict[str, Any]:
    """A valid services.yaml (01 §4 ServiceEntry) with three resolvable services."""
    base: dict[str, Any] = {
        "version": "v2026-01",  # free-form, customer-owned (06 §3)
        "services": [
            {
                "name": "nightly-sync",
                "repos": ["github:acme/nightly-sync"],
                "log_paths": ["logs/nightly-sync"],
                "dags": ["nightly-sync"],
                "cmdb_ci": ["CI1001"],
                "resources": ["db-pool-01", "dag:nightly-sync"],
                "oncall_schedule": "acme-data-oncall",
                "owners": ["alice@acme.test"],
                "depends_on": ["core-db"],
                "paging_integration": "pagerduty:acme-data",
            },
            {
                "name": "core-db",
                "repos": [],
                "log_paths": [],
                "dags": [],
                "cmdb_ci": ["CI2002"],
                "resources": [],
                "oncall_schedule": None,
                "owners": ["bob@acme.test"],
                "depends_on": [],
                "paging_integration": "pagerduty:acme-data",
            },
            {
                "name": "payroll-api",
                "repos": ["github:acme/payroll-api"],
                "log_paths": ["logs/payroll-api"],
                "dags": [],
                "cmdb_ci": ["CI3003"],
                "resources": ["svc:payroll-api"],
                "oncall_schedule": "acme-payroll-oncall",
                "owners": ["carol@acme.test"],
                "depends_on": ["core-db"],
                "paging_integration": "pagerduty:acme-payroll",
            },
        ],
    }
    base.update(overrides)
    return base


def a_classification_file(**overrides: object) -> dict[str, Any]:
    """A valid classification.yaml (ARCH §12) — a subset of the v1.0.0 seed."""
    base: dict[str, Any] = {
        "version": "1.0.0",
        "labels": [
            {"id": "database/saturation", "description": "Pool saturation on a database service."},
            {"id": "database/failure", "description": "Database down or refusing connections."},
            {"id": "deploy/failure", "description": "A release failed or is stuck."},
        ],
    }
    base.update(overrides)
    return base


def a_hypotheses_file(**overrides: object) -> dict[str, Any]:
    """A valid hypotheses.yaml (ARCH §12.1) — a subset of the seed."""
    base: dict[str, Any] = {
        "version": "1.0.0",
        "hypotheses": [
            {"id": "recent_change_push", "excludes": []},
            {"id": "capacity_decay", "excludes": ["recent_change_push"]},
            {"id": "dependency_failure", "excludes": []},
        ],
    }
    base.update(overrides)
    return base


def an_alert(**overrides: object) -> dict[str, Any]:
    """A valid AlertEvent (01 §4) with one field overridable at a time."""
    base: dict[str, Any] = {
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


def a_ticket(**overrides: object) -> dict[str, Any]:
    """A human-filed ticket: origin == 'ticket' with a ticket_number."""
    base = an_alert(
        origin="ticket",
        subject="Customer reports failed payment",
        body="Customer ticket: payment failed twice.",
    )
    base["ticket_number"] = "INC-2026-42"
    base.update(overrides)
    return base


def a_proposal(**overrides: object) -> dict[str, Any]:
    """A valid Proposal (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, Any] = {
        "id": "prop_0001",
        "tenant_id": "acme",
        "case_id": "case_0001",
        "action_kind": ActionKind.WORK_NOTE,
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


def an_approval(**overrides: object) -> dict[str, Any]:
    """A valid Approval (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, Any] = {
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


def a_grant(**overrides: object) -> dict[str, Any]:
    """A valid policy grant (ARCH §6.2) with one field overridable at a time."""
    base: dict[str, Any] = {
        "id": "grnt_0001",
        "tenant_id": "acme",
        "action_kind": ActionKind.WORK_NOTE,
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
