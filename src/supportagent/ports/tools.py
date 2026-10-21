"""First-party read tools (ARCH §7, 01 §3; 11-wp WP-107).

A ``Tool`` declares its trust class, tier and **result schema**; the CEL
checks of RCA runbooks compile against that schema (WP-110). Arguments
reach ``run`` already bound by the registry (``domain.tools.bind_args``):
from catalog fields or reviewed literals, never from ticket text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

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


__all__ = ["Tool", "ToolResult"]
