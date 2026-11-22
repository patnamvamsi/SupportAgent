"""ServiceNow's connector doctor (02 §4.1, WP-M07): Table API reads only, no writes.

The ACL checks read ``sys_security_acl`` / ``sys_security_acl_role`` /
``sys_user_role``. Integration users often can't read those tables. A 403
there is a ``warn`` ("verify manually"), never a silent pass. ACL
conditions and scripts aren't evaluated, so the checks err on the strict
side: a role-less read ACL on ``work_notes`` counts as exposed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import timedelta

from supportagent.app.doctor.base import CheckResult, ConnectorDoctor, DoctorContext
from supportagent.config.schema import ServiceNow
from supportagent.domain.errors import ConfigError, ConnectorError
from supportagent.ports.diagnostics import RecordProbe

_FORBIDDEN = 403
#: ARCH §8.2: who set ``assigned_to`` in the last 30 days.
DISCOVERY_WINDOW = timedelta(days=30)
#: Not human actors, whatever the instance says (ARCH §8.2).
_NOT_HUMAN = frozenset({"system", "guest"})
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
#: BL-030: values per ``fieldIN`` query. A real instance answers 414 (URI too
#: long) when one query lists every ACL of a role-rich user.
IN_CHUNK = 50


def _sn(ctx: DoctorContext) -> ServiceNow:
    """This doctor runs only for a configured ServiceNow (``connector_manifest``)."""
    if ctx.config.servicenow is None:
        raise ConfigError("servicenow is not configured")  # noqa: TRY003
    return ctx.config.servicenow


def _forbidden(exc: Exception) -> bool:
    return isinstance(exc, ConnectorError) and exc.status == _FORBIDDEN


def _in(values: Sequence[str]) -> str:
    return ",".join(sorted(set(values)))


def _read_in(  # noqa: PLR0913 — one Table API read, split by its IN list
    sn: RecordProbe,
    table: str,
    field: str,
    values: Sequence[str],
    *,
    fields: Sequence[str],
    limit: int,
    extra: str = "",
) -> list[dict[str, str]]:
    """``field IN values`` (plus ``extra``), read ``IN_CHUNK`` values at a time (BL-030)."""
    ordered = sorted(set(values))
    rows: list[dict[str, str]] = []
    for start in range(0, len(ordered), IN_CHUNK):
        query = f"{field}IN{','.join(ordered[start : start + IN_CHUNK])}" + (
            f"^{extra}" if extra else ""
        )
        rows.extend(sn.read(table, query=query, fields=fields, limit=limit).rows)
    return rows


def _role_names(sn: RecordProbe, role_ids: Sequence[str]) -> dict[str, str]:
    rows = _read_in(sn, "sys_user_role", "sys_id", role_ids, fields=["sys_id", "name"], limit=500)
    return {r["sys_id"]: r.get("name", "") for r in rows}


class AuthCheck:
    """Token obtained; a read probe on each configured table succeeds; no write."""

    name = "servicenow.auth"

    def run(self, ctx: DoctorContext) -> CheckResult:
        tables = _sn(ctx).tables
        for table in tables:
            ctx.records.read(table, query="", fields=["sys_id"], limit=1)
        return CheckResult(
            self.name,
            "pass",
            f"authenticated as {_sn(ctx).auth.kind}; read probe ok on " + ", ".join(tables),
        )


class JournalVisibilityCheck:
    """``work_notes`` is not readable by external (customer / ESS) users."""

    name = "servicenow.journal_visibility"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn = ctx.records
        names = sorted({f"{t}.work_notes" for t in _sn(ctx).tables} | {"task.work_notes"})
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
            links = _read_in(
                sn,
                "sys_security_acl_role",
                "sys_security_acl",
                [a["sys_id"] for a in acls],
                fields=["sys_security_acl", "sys_user_role"],
                limit=500,
            )
            names_by_id = _role_names(sn, [link["sys_user_role"] for link in links])
        except ConnectorError as exc:
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
        sn = ctx.records
        user_name = _sn(ctx).integration_user
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
        except ConnectorError as exc:
            if _forbidden(exc):
                return CheckResult(
                    self.name,
                    "warn",
                    "can't read roles or ACLs (403): review the user's roles manually",
                )
            raise
        # BL-030: a non-admin integration user usually can't read the ACL
        # tables; the role findings still stand without them.
        try:
            write_acls: list[str] | None = self._write_acls(sn, role_ids)
        except ConnectorError as exc:
            if not _forbidden(exc):
                raise
            write_acls = None
        allowed = set(_sn(ctx).tables)
        extra = sorted(
            name
            for name in write_acls or []
            if name.split(".", 1)[0] not in allowed and name not in _TASK_JOURNAL_ACLS
        )
        broad = sorted(set(roles.values()) & BROAD_ROLES)
        # BL-030: ServiceNow hides elevated roles (security_admin) from a
        # non-admin reader, so an unnamed role is reported, never dropped.
        hidden = len(set(role_ids) - set(roles))
        if broad or extra or hidden or write_acls is None:
            parts = []
            if broad:
                parts.append("holds " + ", ".join(broad))
            if hidden:
                parts.append(
                    f"{hidden} role(s) it can't read (hidden roles are usually elevated, "
                    "e.g. security_admin): remove them"
                )
            if extra:
                parts.append(f"{len(extra)} write ACL(s) outside {', '.join(sorted(allowed))}")
            if write_acls is None:
                parts.append("can't read ACLs (403): review its write access manually")
            return CheckResult(
                self.name,
                "warn",
                f"{user_name}: " + "; ".join(parts),
                {
                    "broad_roles": broad,
                    "extra_write_acls": extra,
                    "acls_readable": write_acls is not None,
                    "hidden_roles": hidden,
                },
            )
        return CheckResult(
            self.name,
            "pass",
            f"{user_name}: roles {', '.join(sorted(roles.values())) or 'none'}; "
            "writes limited to the configured tables",
        )

    @staticmethod
    def _write_acls(sn: RecordProbe, role_ids: Sequence[str]) -> list[str]:
        acl_ids = [
            r["sys_security_acl"]
            for r in _read_in(
                sn,
                "sys_security_acl_role",
                "sys_user_role",
                role_ids,
                fields=["sys_security_acl"],
                limit=1000,
            )
        ]
        rows = _read_in(
            sn,
            "sys_security_acl",
            "sys_id",
            acl_ids,
            fields=["name"],
            limit=1000,
            extra="operationINwrite,create,delete^active=true",
        )
        return [r["name"] for r in rows]


class RateLimitCheck:
    """Headroom from the ``X-RateLimit-*`` headers; recommends ``poll_interval_s``."""

    name = "servicenow.rate_limit"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn_cfg = _sn(ctx)
        probe = ctx.records.read(sn_cfg.tables[0], query="", fields=["sys_id"], limit=1)
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


class HistoryAccessCheck:
    """BL-030: sync can read the per-change history it needs (``sys_audit``, journal).

    Sync reads ``sys_audit`` and ``sys_journal_field`` for every changed record
    to spot a human takeover (02 §6 ``history``). ServiceNow restricts both
    tables by default: a 403 means the poll job fails on the first update,
    and an empty journal can hide human work notes.
    """

    name = "servicenow.history_access"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sn = ctx.records
        table = _sn(ctx).tables[0]
        blocked: list[str] = []
        journal_rows = 0
        for history_table, query in (
            ("sys_audit", f"tablename={table}"),
            ("sys_journal_field", f"name={table}"),
        ):
            try:
                rows = sn.read(history_table, query=query, fields=["sys_id"], limit=1).rows
            except ConnectorError as exc:
                if _forbidden(exc):
                    blocked.append(history_table)
                    continue
                raise
            if history_table == "sys_journal_field":
                journal_rows = len(rows)
        if blocked:
            return CheckResult(
                self.name,
                "fail",
                f"the integration user can't read {', '.join(blocked)} (403): sync can't detect "
                "a human takeover and the poll job fails on the first update; grant it read "
                "access (a read ACL on each table for a dedicated role)",
                {"blocked": blocked},
            )
        if journal_rows == 0:
            return CheckResult(
                self.name,
                "warn",
                f"no {table} journal entries readable: if records have work notes or comments, "
                "a read ACL on sys_journal_field is hiding them from sync",
            )
        return CheckResult(
            self.name, "pass", f"sys_audit and sys_journal_field readable for {table}"
        )


class AuditRetentionCheck:
    """Reports the ``sys_audit`` retention window (warning if < 7 days)."""

    name = "servicenow.audit_retention"

    def run(self, ctx: DoctorContext) -> CheckResult:
        try:
            rows = ctx.records.read(
                "sys_auto_flush",
                query="tablename=sys_audit^active=true",
                fields=["age"],
                limit=10,
            ).rows
        except ConnectorError as exc:
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


def discover_automation_actors(ctx: DoctorContext) -> list[tuple[str, str]]:
    """ARCH §8.2: recent ``assigned_to`` setters that can't log in interactively."""
    sn_cfg = _sn(ctx)
    since = (ctx.clock.now() - DISCOVERY_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    audits = ctx.records.read(
        "sys_audit",
        query=(
            f"fieldname=assigned_to^tablenameIN{','.join(sn_cfg.tables)}^sys_created_on>={since}"
        ),
        fields=["user"],
        limit=10_000,
    ).rows
    actors = sorted(
        {a["user"] for a in audits if a.get("user")} - _NOT_HUMAN - {sn_cfg.integration_user}
    )
    if not actors:
        return []
    users = ctx.records.read(
        "sys_user",
        query=f"user_nameIN{','.join(actors)}",
        fields=["user_name", "web_service_access_only", "internal_integration_user"],
        limit=len(actors),
    ).rows
    found: list[tuple[str, str]] = []
    for user in users:
        if user.get("internal_integration_user") == "true":
            found.append((user["user_name"], "integration"))
        elif user.get("web_service_access_only") == "true":
            found.append((user["user_name"], "web_service"))
    return sorted(found)


#: WP-M07: ServiceNow's doctor checks, in the 02 §4.1 order.
SERVICENOW_DOCTOR = ConnectorDoctor(
    name="servicenow",
    checks=(
        AuthCheck(),
        JournalVisibilityCheck(),
        IntegrationRoleCheck(),
        RateLimitCheck(),
        HistoryAccessCheck(),
        AuditRetentionCheck(),
    ),
    discover_automation=discover_automation_actors,
)


__all__ = [
    "SERVICENOW_DOCTOR",
    "AuditRetentionCheck",
    "AuthCheck",
    "HistoryAccessCheck",
    "IntegrationRoleCheck",
    "JournalVisibilityCheck",
    "RateLimitCheck",
    "discover_automation_actors",
]
