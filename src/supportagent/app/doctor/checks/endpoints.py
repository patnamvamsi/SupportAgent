"""Endpoint checks (02 §4.1): ``automation_actors``, ``llm``, ``oidc``."""

from __future__ import annotations

from datetime import timedelta

from supportagent.app.doctor.base import CheckResult, DoctorContext

#: ARCH §8.2: who set ``assigned_to`` in the last 30 days.
DISCOVERY_WINDOW = timedelta(days=30)
#: Not human actors, whatever the instance says (ARCH §8.2).
_NOT_HUMAN = frozenset({"system", "guest"})


class AutomationActorsCheck:
    """Non-interactive ``sys_user``s that set ``assigned_to``; confirmed before assisted mode.

    With ``--discover-automation`` the candidates are written to
    ``automation_actors`` with ``confirmed_by`` NULL (an admin confirms them).
    Shadow mode never acts on takeover, so unconfirmed candidates only block
    the move to ``mode: assisted``.
    """

    name = "automation_actors"

    def run(self, ctx: DoctorContext) -> CheckResult:
        tenant = ctx.config.tenant.id
        added = 0
        found = 0
        if ctx.discover_automation:
            candidates = self._discover(ctx)
            found = len(candidates)
            added = ctx.automation.add_candidates(tenant, candidates)
        total, confirmed = ctx.automation.summary(tenant)
        unconfirmed = total - confirmed
        prefix = (
            f"discovered {found} non-interactive actor(s) ({added} new); "
            if ctx.discover_automation
            else ""
        )
        detail = f"{prefix}{total} candidate(s), {confirmed} confirmed"
        data = {"candidates": total, "confirmed": confirmed, "discovered": found, "added": added}
        if ctx.config.mode != "shadow" and unconfirmed:
            return CheckResult(
                self.name,
                "fail",
                f"{detail}: an admin must confirm them before mode: {ctx.config.mode}",
                data,
            )
        if not ctx.discover_automation and total == 0:
            detail += "; run `doctor --discover-automation` before moving to assisted mode"
        return CheckResult(self.name, "pass", detail, data)

    @staticmethod
    def _discover(ctx: DoctorContext) -> list[tuple[str, str]]:
        sn_cfg = ctx.config.servicenow
        since = (ctx.clock.now() - DISCOVERY_WINDOW).strftime("%Y-%m-%d %H:%M:%S")
        audits = ctx.servicenow.read(
            "sys_audit",
            query=(
                f"fieldname=assigned_to^tablenameIN{','.join(sn_cfg.tables)}"
                f"^sys_created_on>={since}"
            ),
            fields=["user"],
            limit=10_000,
        ).rows
        actors = sorted(
            {a["user"] for a in audits if a.get("user")} - _NOT_HUMAN - {sn_cfg.integration_user}
        )
        if not actors:
            return []
        users = ctx.servicenow.read(
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


class LLMCheck:
    """Each slot's model id resolves; a 1-token probe succeeds; no floating alias."""

    name = "llm"

    def run(self, ctx: DoctorContext) -> CheckResult:
        resolved: dict[str, str] = {}
        mismatched: list[str] = []
        for slot, spec in sorted(ctx.config.llm.slots.items()):
            got = ctx.llm.probe(spec.model_id)
            resolved[slot] = got
            if got != spec.model_id:
                mismatched.append(f"{slot}: {spec.model_id} resolved to {got}")
        if mismatched:
            return CheckResult(
                self.name,
                "fail",
                "pinned snapshot not served as configured: " + "; ".join(mismatched),
                {"resolved": resolved},
            )
        return CheckResult(
            self.name,
            "pass",
            f"{ctx.config.llm.provider}: "
            + ", ".join(f"{slot}={model}" for slot, model in resolved.items()),
            {"resolved": resolved},
        )


class OIDCCheck:
    """The issuer's discovery document is reachable (and names the configured issuer)."""

    name = "oidc"

    def run(self, ctx: DoctorContext) -> CheckResult:
        issuer = ctx.identity.discover()
        return CheckResult(self.name, "pass", f"discovery ok for {issuer}")


__all__ = ["AutomationActorsCheck", "LLMCheck", "OIDCCheck"]
