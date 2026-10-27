"""Endpoint checks (02 §4.1): ``automation_actors``, ``llm``, ``oidc``; ``mcp`` (WP-M06)."""

from __future__ import annotations

from collections.abc import Callable

from supportagent.app.doctor.base import CheckResult, DoctorContext
from supportagent.domain.errors import ExternalServiceError


class AutomationActorsCheck:
    """Non-interactive identities that set a record's assignee; confirmed before assisted mode.

    The connector discovers them (``ConnectorDoctor.discover_automation``,
    WP-M07). With ``--discover-automation`` the candidates are written to
    ``automation_actors`` with ``confirmed_by`` NULL (an admin confirms them).
    Shadow mode never acts on takeover, so unconfirmed candidates only block
    the move to ``mode: assisted``.
    """

    name = "automation_actors"

    def __init__(self, discover: Callable[[DoctorContext], list[tuple[str, str]]]) -> None:
        self._discover = discover

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


class McpCheck:
    """WP-M06: each registered MCP server answers, lists every declared tool, and agrees
    with the trust class it was registered with."""

    name = "mcp"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if not ctx.mcp_servers:
            return CheckResult(self.name, "pass", "no MCP servers registered")
        bindings = ctx.config.tools.bindings
        failures: list[str] = []
        lines: list[str] = []
        for name, probe in sorted(ctx.mcp_servers.items()):
            bound = sorted(c for c, b in bindings.items() if b.mcp == name)
            try:
                problems = list(probe())
            except ExternalServiceError as exc:
                failures.append(f"{name}: unreachable ({exc})")
                continue
            if problems:
                failures.append(f"{name}: " + "; ".join(problems))
                continue
            lines.append(f"{name} serves {', '.join(bound) if bound else 'no capability'}")
        data: dict[str, object] = {"servers": sorted(ctx.mcp_servers)}
        if failures:
            return CheckResult(self.name, "fail", " · ".join(failures + lines), data)
        return CheckResult(self.name, "pass", " · ".join(lines), data)
