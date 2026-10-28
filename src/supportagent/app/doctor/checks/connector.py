"""The ``connector`` check (WP-M07): the connector's capabilities against the mode."""

from __future__ import annotations

from supportagent.app.doctor.base import CheckResult, DoctorContext
from supportagent.domain.connector import Capability, limits_for


class ConnectorCheck:
    """``fail`` when the mode needs a capability the connector lacks; ``warn`` in shadow."""

    name = "connector"

    def run(self, ctx: DoctorContext) -> CheckResult:
        manifest = ctx.connector
        limits = limits_for(manifest)
        caps = sorted(c.value for c in manifest.capabilities)
        missing = sorted(c.value for c in Capability if c not in manifest.capabilities)
        data = {"connector": manifest.name, "capabilities": caps, "missing": missing}
        if not limits.reasons:
            return CheckResult(self.name, "pass", f"{manifest.name}: every capability", data)
        detail = f"{manifest.name}: " + "; ".join(limits.reasons)
        if ctx.config.mode != "shadow" and not limits.autonomous:
            return CheckResult(self.name, "fail", f"{detail} (mode: {ctx.config.mode})", data)
        return CheckResult(self.name, "warn", detail, data)


__all__ = ["ConnectorCheck"]
