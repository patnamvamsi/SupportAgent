"""Routing table models (ARCH §4).

The ``DomainModel`` subclasses for first-match routing with a default route.
Re-exported by :mod:`supportagent.domain.routing` to preserve that public
import path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity


class Route(DomainModel):
    """A concrete route: the actions to take (and its audit ``why``).

    ``actions`` may be empty in the domain; the config layer enforces at least
    one. ``rca`` is fixed to ``"none"`` in v0.0.
    """

    id: str = Field(min_length=1)
    actions: list[ActionKind] = Field(default_factory=list)
    rca: Literal["none"] = "none"
    comms: str | None = None
    why: str | None = None


class RoutingRule(DomainModel):
    """One routing rule: a classification + the severities it applies to."""

    match_classification: str = Field(min_length=1)
    severities: list[Severity] = Field(default_factory=list)
    route: Route


class RoutingTable(DomainModel):
    """Ordered routing rules plus the fallback (generic) route (ARCH §4)."""

    rules: list[RoutingRule] = Field(default_factory=list)
    default: Route

    def lookup(self, label: str, severity: Severity) -> Route:
        """The first rule matching ``label`` and ``severity``; else ``default``.

        Never raises: a routing miss always falls through to the default route.
        """
        for rule in self.rules:
            if rule.match_classification == label and severity in rule.severities:
                return rule.route
        return self.default
