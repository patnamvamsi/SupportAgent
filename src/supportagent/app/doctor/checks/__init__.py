"""The doctor registry (02 §4.1): one class per check, in the table's order."""

from __future__ import annotations

from supportagent.app.doctor.base import Check
from supportagent.app.doctor.checks.endpoints import AutomationActorsCheck, LLMCheck, OIDCCheck
from supportagent.app.doctor.checks.local import ConfigCheck, DbCheck, PagingCheck
from supportagent.app.doctor.checks.servicenow import (
    AuditRetentionCheck,
    AuthCheck,
    HistoryAccessCheck,
    IntegrationRoleCheck,
    JournalVisibilityCheck,
    RateLimitCheck,
)


def default_checks() -> list[Check]:
    """Every v0.0 check, in the 02 §4.1 order."""
    return [
        ConfigCheck(),
        DbCheck(),
        AuthCheck(),
        JournalVisibilityCheck(),
        IntegrationRoleCheck(),
        RateLimitCheck(),
        HistoryAccessCheck(),
        AuditRetentionCheck(),
        AutomationActorsCheck(),
        PagingCheck(),
        LLMCheck(),
        OIDCCheck(),
    ]


__all__ = ["default_checks"]
