"""No ticket connector (ADR-0022): a shadow deployment triaging webhook alerts only."""

from __future__ import annotations

from supportagent.app.doctor.base import CheckResult, ConnectorDoctor, DoctorContext


class AlertIntakeCheck:
    """``pass`` naming the webhook sources that are the deployment's only intake."""

    name = "none.alert_intake"

    def run(self, ctx: DoctorContext) -> CheckResult:
        sources = sorted(w.source for w in ctx.config.webhooks)
        return CheckResult(
            self.name,
            "pass",
            f"no ticket connector: alerts arrive by webhook ({', '.join(sources)}); shadow only",
            {"sources": sources},
        )


NO_CONNECTOR_DOCTOR = ConnectorDoctor(
    name="none",
    checks=(AlertIntakeCheck(),),
    discover_automation=lambda ctx: [],
)

__all__ = ["NO_CONNECTOR_DOCTOR", "AlertIntakeCheck"]
