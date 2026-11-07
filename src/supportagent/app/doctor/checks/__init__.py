"""The doctor registry (02 §4.1): one class per check, in the table's order."""

from __future__ import annotations

from supportagent.app.doctor.base import Check
from supportagent.app.doctor.checks.connector import ConnectorCheck
from supportagent.app.doctor.checks.endpoints import (
    AutomationActorsCheck,
    LLMCheck,
    McpCheck,
    OIDCCheck,
)
from supportagent.app.doctor.checks.local import (
    ConfigCheck,
    DbCheck,
    KnowledgeCheck,
    OnboardingCheck,
    PacksCheck,
    PagingCheck,
    PiiCheck,
)
from supportagent.app.doctor.connectors import connector_doctor


def default_checks(connector: str = "servicenow") -> list[Check]:
    """Every check, in the 02 §4.1 order; the connector's own checks come after the DB.

    WP-M07: ``connector`` picks the registered connector doctor; the generic
    ``connector`` check (capabilities against the mode) follows its checks.
    """
    plugin = connector_doctor(connector)
    return [
        ConfigCheck(),
        PacksCheck(),
        OnboardingCheck(),
        KnowledgeCheck(),
        PiiCheck(),
        DbCheck(),
        *plugin.checks,
        ConnectorCheck(),
        AutomationActorsCheck(plugin.discover_automation),
        PagingCheck(),
        LLMCheck(),
        McpCheck(),
        OIDCCheck(),
    ]


__all__ = ["default_checks"]
