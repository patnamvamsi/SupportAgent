"""Routing table: first matching rule wins, else the default (ARCH §4).

The ``DomainModel`` subclasses are defined in
:mod:`supportagent.domain.model.routing` (mypy convention: pydantic models
live in the ``domain.model`` package). This module preserves the public
``supportagent.domain.routing`` import path.
"""

from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable

__all__ = ["Route", "RoutingRule", "RoutingTable"]
