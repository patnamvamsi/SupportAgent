"""A system's readiness bar and per-class gates (ADR-0015 §5, decision 6; WP-O6b).

Set per system in ``system.yaml`` ``readiness:``; every number is provisional
until the system's own history sets it (ADR-0003).
"""

from __future__ import annotations

from pydantic import Field

from supportagent.domain.model.base import DomainModel


class ReadinessGates(DomainModel):
    #: Scenarios with evidence a class needs before its rates mean anything.
    min_scenarios: int = Field(default=3, ge=1)
    #: The share of its scenarios where the tools must support the class's cause.
    min_top1: float = Field(default=0.75, ge=0, le=1)
    #: False causes a class may have and still be ready: none.
    max_false_causes: int = Field(default=0, ge=0)
    #: The share of incident weight ready classes must hold (decision 6: 70% for now).
    bar: float = Field(default=0.70, ge=0, le=1)


__all__ = ["ReadinessGates"]
