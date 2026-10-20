"""ServiceNow checks (02 §4.1). Every probe is a Table API read; nothing is written.

The ACL checks read ``sys_security_acl`` / ``sys_security_acl_role`` /
``sys_user_role``. Integration users often can't read those tables. A 403
there is a ``warn`` ("verify manually"), never a silent pass. ACL
conditions and scripts aren't evaluated, so the checks err on the strict
side: a role-less read ACL on ``work_notes`` counts as exposed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from supportagent.app.doctor.base import CheckResult, DoctorContext
from supportagent.domain.errors import ServiceNowError
from supportagent.ports.diagnostics import ServiceNowProbe

_FORBIDDEN = 403
#: Roles whose holders are customers / self-service users (ESS has no role).
EXTERNAL_ROLES = frozenset({"snc_external"})
#: Roles that can write anything; an integration user should never hold them.
BROAD_ROLES = frozenset({"admin", "security_admin"})
#: Journal fields the v0.0 actions write, inherited from ``task``.
_TASK_JOURNAL_ACLS = frozenset({"task.work_notes", "task.comments"})
#: (prov.) Table API calls one poll costs: the page, plus history for changed records.
CALLS_PER_POLL = 3
#: (prov.) Polling may use at most this share of the hourly rate limit.
POLL_BUDGET_SHARE = 0.5
LOW_HEADROOM = 0.2
MIN_AUDIT_DAYS = 7


def _forbidden(exc: Exception) -> bool:
    return isinstance(exc, ServiceNowError) and exc.status == _FORBIDDEN


def _in(values: Sequence[str]) -> str:
    return ",".join(sorted(set(values)))


def _role_names(sn: ServiceNowProbe, role_ids: Sequence[str]) -> dict[str, str]:
    if not role_ids:
        return {}
    rows = sn.read(
        "sys_user_role", query=f"sys_idIN{_in(role_ids)}", fields=["sys_id", "name"], limit=500
    ).rows
    return {r["sys_id"]: r.get("name", "") for r in rows}


class AuthCheck:
    """Token obtained; a read probe on each configured table succeeds; no write."""

    name = "servicenow.auth"

    def run(self, ctx: DoctorContext) -> CheckResult:
        tables = ctx.config.servicenow.tables
        for table in tables:
            ctx.servicenow.read(table, query="", fields=["sys_id"], limit=1)
        return CheckResult(
            self.name,
            "pass",
            f"authenticated as {ctx.config.servicenow.auth.kind}; read probe ok on "
            + ", ".join(tables),
        )


class JournalVisibilityCheck:
    """``work_notes`` is not readable by external (customer / ESS) users."""

    name = "servicenow.journal_visibility"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn = ctx.servicenow
        names = sorted(
            {f"{t}.work_notes" for t in ctx.config.servicenow.tables} | {"task.work_notes"}
        )
        try:
            acls = sn.read(
                "sys_security_acl",
                query=f"nameIN{_in(names)}^operation=read^active=true",
                fields=["sys_id", "name"],
                limit=100,
            ).rows
            if not acls:
                return CheckResult(
                    self.name,
                    "fail",
                    "no active read ACL protects work_notes: every user can read it",
                )
            links = sn.read(
                "sys_security_acl_role",
                query=f"sys_security_aclIN{_in([a['sys_id'] for a in acls])}",
                fields=["sys_security_acl", "sys_user_role"],
                limit=500,
            ).rows
            names_by_id = _role_names(sn, [link["sys_user_role"] for link in links])
        except ServiceNowError as exc:
            if _forbidden(exc):
                return CheckResult(
                    self.name,
                    "warn",
                    "the integration user can't read ACLs (403): verify manually that "
                    "work_notes is not visible to snc_external / self-service users",
                )
            raise
        exposed: list[str] = []
        for acl in acls:
            roles = {
                names_by_id.get(link["sys_user_role"], "")
                for link in links
                if link["sys_security_acl"] == acl["sys_id"]
            }
            if not roles:
                exposed.append(f"{acl['name']} (no role required)")
            elif roles & EXTERNAL_ROLES:
                exposed.append(
                    f"{acl['name']} (grants {', '.join(sorted(roles & EXTERNAL_ROLES))})"
                )
        if exposed:
            return CheckResult(
                self.name,
                "fail",
                "work_notes readable by external users via " + "; ".join(exposed),
                {"exposed": exposed},
            )
        return CheckResult(
            self.name, "pass", f"{len(acls)} read ACL(s) on work_notes; none open to external users"
        )


class IntegrationRoleCheck:
    """The integration user holds no write ACLs beyond the configured tables (warning)."""

    name = "servicenow.integration_role"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn = ctx.servicenow
        user_name = ctx.config.servicenow.integration_user
        users = sn.read("sys_user", query=f"user_name={user_name}", fields=["sys_id"], limit=1).rows
        if not users:
            return CheckResult(
                self.name,
                "fail",
                f"integration user {user_name!r} not found: the echo filter would drop nothing",
            )
        try:
            role_ids = [
                r["role"]
                for r in sn.read(
                    "sys_user_has_role",
                    query=f"user={users[0]['sys_id']}^state=active",
                    fields=["role"],
                    limit=500,
                ).rows
            ]
            roles = _role_names(sn, role_ids)
            write_acls = self._write_acls(sn, role_ids)
        except ServiceNowError as exc:
            if _forbidden(exc):
                return CheckResult(
                    self.name,
                    "warn",
                    "can't read roles or ACLs (403): review the user's roles manually",
                )
            raise
        allowed = set(ctx.config.servicenow.tables)
        extra = sorted(
            name
            for name in write_acls
            if name.split(".", 1)[0] not in allowed and name not in _TASK_JOURNAL_ACLS
        )
        broad = sorted(set(roles.values()) & BROAD_ROLES)
        if broad or extra:
            parts = []
            if broad:
                parts.append("holds " + ", ".join(broad))
            if extra:
                parts.append(f"{len(extra)} write ACL(s) outside {', '.join(sorted(allowed))}")
            return CheckResult(
                self.name,
                "warn",
                f"{user_name}: " + "; ".join(parts),
                {"broad_roles": broad, "extra_write_acls": extra},
            )
        return CheckResult(
            self.name,
            "pass",
            f"{user_name}: roles {', '.join(sorted(roles.values())) or 'none'}; "
            "writes limited to the configured tables",
        )

    @staticmethod
    def _write_acls(sn: ServiceNowProbe, role_ids: Sequence[str]) -> list[str]:
        if not role_ids:
            return []
        acl_ids = [
            r["sys_security_acl"]
            for r in sn.read(
                "sys_security_acl_role",
                query=f"sys_user_roleIN{_in(role_ids)}",
                fields=["sys_security_acl"],
                limit=1000,
            ).rows
        ]
        if not acl_ids:
            return []
        rows = sn.read(
            "sys_security_acl",
            query=f"sys_idIN{_in(acl_ids)}^operationINwrite,create,delete^active=true",
            fields=["name"],
            limit=1000,
        ).rows
        return [r["name"] for r in rows]


class RateLimitCheck:
    """Headroom from the ``X-RateLimit-*`` headers; recommends ``poll_interval_s``."""

    name = "servicenow.rate_limit"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn_cfg = ctx.config.servicenow
        probe = ctx.servicenow.read(sn_cfg.tables[0], query="", fields=["sys_id"], limit=1)
        limit_s = probe.headers.get("x-ratelimit-limit")
        remaining_s = probe.headers.get("x-ratelimit-remaining")
        if not limit_s or not limit_s.isdigit():
            return CheckResult(
                self.name,
                "pass",
                "no rate-limit rule applies to the integration user (no X-RateLimit headers); "
                f"poll_interval_s={sn_cfg.poll_interval_s} kept",
            )
        limit = int(limit_s)
        remaining = int(remaining_s) if remaining_s and remaining_s.isdigit() else limit
        # Hourly limit (ServiceNow rate-limit rules are per hour).
        needed = len(sn_cfg.tables) * CALLS_PER_POLL * 3600 / (POLL_BUDGET_SHARE * limit)
        recommended = max(1, math.ceil(needed))
        data: dict[str, object] = {
            "limit_per_hour": limit,
            "remaining": remaining,
            "recommended_poll_interval_s": recommended,
        }
        headroom = remaining / limit if limit else 0.0
        if headroom < LOW_HEADROOM:
            return CheckResult(
                self.name,
                "warn",
                f"only {remaining}/{limit} requests left this hour; other integrations share "
                f"the budget. Recommended poll_interval_s >= {recommended}",
                data,
            )
        if recommended > sn_cfg.poll_interval_s:
            return CheckResult(
                self.name,
                "warn",
                f"limit {limit}/h: poll_interval_s={sn_cfg.poll_interval_s} would spend over "
                f"{POLL_BUDGET_SHARE:.0%} of it; raise it to {recommended}",
                data,
            )
        return CheckResult(
            self.name,
            "pass",
            f"limit {limit}/h, {remaining} left; poll_interval_s={sn_cfg.poll_interval_s} "
            f"fits (minimum {recommended})",
            data,
        )


class AuditRetentionCheck:
    """Reports the ``sys_audit`` retention window (warning if < 7 days)."""

    name = "servicenow.audit_retention"

    def run(self, ctx: DoctorContext) -> CheckResult:
        try:
            rows = ctx.servicenow.read(
                "sys_auto_flush",
                query="tablename=sys_audit^active=true",
                fields=["age"],
                limit=10,
            ).rows
        except ServiceNowError as exc:
            if _forbidden(exc):
                return CheckResult(
                    self.name, "warn", "can't read sys_auto_flush (403): check sys_audit retention"
                )
            raise
        ages = [int(r["age"]) for r in rows if r.get("age", "").isdigit()]
        if not ages:
            return CheckResult(
                self.name, "pass", "no auto-flush rule on sys_audit: history is kept indefinitely"
            )
        days = min(ages) / 86_400
        data = {"retention_days": round(days, 1)}
        if days < MIN_AUDIT_DAYS:
            return CheckResult(
                self.name,
                "warn",
                f"sys_audit is flushed after {days:.1f} days (< {MIN_AUDIT_DAYS}): changes older "
                "than that can't be replayed after an outage",
                data,
            )
        return CheckResult(self.name, "pass", f"sys_audit kept {days:.0f} days", data)


__all__ = [
    "AuditRetentionCheck",
    "AuthCheck",
    "IntegrationRoleCheck",
    "JournalVisibilityCheck",
    "RateLimitCheck",
]
