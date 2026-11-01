"""First-party read tools (ARCH §7, 01 §3; 11-wp WP-107).

A ``Tool`` declares its trust class, tier and **result schema**; the CEL
checks of RCA runbooks compile against that schema (WP-110). Arguments
reach ``run`` already bound by the registry (``domain.tools.bind_args``):
from catalog fields or reviewed literals, never from ticket text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from supportagent.domain.tiers import Tier, TrustClass


@dataclass(frozen=True, slots=True)
class ToolResult:
    data: Mapping[str, object]
    truncated: bool = False
    meta: Mapping[str, object] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    trust_class: TrustClass
    tier: Tier
    result_schema: Mapping[str, object]
    #: Argument keys that may take a reviewed literal (e.g. a grep pattern); every
    #: other argument must be bound from ``$service.<field>``.
    literal_args: frozenset[str]

    def run(self, args: Mapping[str, object], *, timeout_s: float) -> ToolResult:
        """One call; raises ``ToolError`` (typed retryability, 01 §6)."""
        ...


@runtime_checkable
class ReplayableTool(Protocol):
    """ADR-0018: a tool that can read the past, the same read ending at ``at``.

    Point-in-time tools (current locks, pods, queue depth) don't implement it:
    their evidence for a past incident is *not replayable*.
    """

    def run_as_of(
        self, args: Mapping[str, object], *, at: datetime, timeout_s: float
    ) -> ToolResult: ...


__all__ = ["ReplayableTool", "Tool", "ToolResult"]
