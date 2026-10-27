"""Shared doctor fixtures: a healthy FakeServiceNowHTTP instance (02 §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime

from supportagent.adapters.fakes.servicenow import FakeServiceNowHTTP

INTEGRATION_USER = "svc_supportagent"
WHEN = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def healthy_instance(sn: FakeServiceNowHTTP) -> FakeServiceNowHTTP:
    """Every v0.0 doctor check passes against this instance."""
    sn.seed_record("incident", "inc-1", number="INC0010001", caller_id="u1", updated_at=WHEN)
    sn.seed_row("sys_user", {"sys_id": "u-svc", "user_name": INTEGRATION_USER})
    sn.seed_row("sys_user_role", {"sys_id": "r-itil", "name": "itil"})
    sn.seed_row("sys_user_role", {"sys_id": "r-ext", "name": "snc_external"})
    sn.seed_row(
        "sys_user_has_role",
        {"sys_id": "uhr-1", "user": "u-svc", "role": "r-itil", "state": "active"},
    )
    # work_notes: one read ACL, internal (itil) only.
    sn.seed_row(
        "sys_security_acl",
        {"sys_id": "acl-wn", "name": "task.work_notes", "operation": "read", "active": "true"},
    )
    sn.seed_row(
        "sys_security_acl_role",
        {"sys_id": "ar-1", "sys_security_acl": "acl-wn", "sys_user_role": "r-itil"},
    )
    # The integration user's write ACLs stay on incident.
    sn.seed_row(
        "sys_security_acl",
        {"sys_id": "acl-w", "name": "incident", "operation": "write", "active": "true"},
    )
    sn.seed_row(
        "sys_security_acl_role",
        {"sys_id": "ar-2", "sys_security_acl": "acl-w", "sys_user_role": "r-itil"},
    )
    sn.seed_row(
        "sys_auto_flush",
        {"sys_id": "af-1", "tablename": "sys_audit", "age": str(90 * 86_400), "active": "true"},
    )
    # BL-030: sync's history tables are readable, with one human work note.
    sn.seed_journal("incident", "inc-1", user="ada", ts=WHEN, element="work_notes", value="looking")
    sn.set_response_headers({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4900"})
    return sn
