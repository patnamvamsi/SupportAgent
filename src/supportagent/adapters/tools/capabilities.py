"""Tool capabilities (ADR-0011 §2, WP-M06): one result schema per capability.

Packs name capabilities (``metrics.query``, ``postgres.activity``); a
deployment binds each to a backend: the first-party adapter, or a tool on a
registered MCP server. The schema and the literal-argument keys belong to the
capability, not the backend, so a pack's checks compile once and run the
same on either (``test_binding_result_validated_against_capability_schema``).

The first-party adapters declare them today; this module is the catalog.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from supportagent.adapters.tools.airflow import AirflowTool
from supportagent.adapters.tools.changes import RecentChangesTool
from supportagent.adapters.tools.cron import CronStatusTool
from supportagent.adapters.tools.gha import GitHubActionsTool
from supportagent.adapters.tools.kubernetes import PodStatusTool
from supportagent.adapters.tools.logs import LogsTool
from supportagent.adapters.tools.oncall import OnCallTool
from supportagent.adapters.tools.postgres import POSTGRES_TOOLS
from supportagent.adapters.tools.prometheus import PrometheusQueryTool


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    result_schema: Mapping[str, object]
    literal_args: frozenset[str]


class _Declares(Protocol):
    name: str
    result_schema: Mapping[str, object]
    literal_args: frozenset[str]


_FIRST_PARTY: tuple[type[_Declares], ...] = (
    LogsTool,
    CronStatusTool,
    AirflowTool,
    GitHubActionsTool,
    OnCallTool,
    PrometheusQueryTool,
    PodStatusTool,
    RecentChangesTool,
    *POSTGRES_TOOLS,
)

CAPABILITIES: Mapping[str, Capability] = {
    cls.name: Capability(cls.name, cls.result_schema, cls.literal_args) for cls in _FIRST_PARTY
}


__all__ = ["CAPABILITIES", "Capability"]
