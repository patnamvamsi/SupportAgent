"""WP-010: routing table lookup — first match wins, else the default (ARCH §4).

Canonical test IDs from 10-wp §WP-010:
``test_routing_first_match_wins``, ``test_routing_default_when_no_match``.
"""

from __future__ import annotations

from supportagent.domain.kinds import ActionKind
from supportagent.domain.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.severity import Severity


def _route(id: str) -> Route:
    return Route(id=id, actions=[ActionKind.WORK_NOTE])


def test_routing_first_match_wins() -> None:
    # ARCH §4: lookup returns the first rule whose classification and severity
    # both match — order is significant.
    table = RoutingTable(
        rules=[
            RoutingRule(
                match_classification="database/saturation",
                severities=[Severity.P1, Severity.P2],
                route=_route("db-sat-first"),
            ),
            RoutingRule(
                match_classification="database/saturation",
                severities=[Severity.P1, Severity.P2],
                route=_route("db-sat-second"),
            ),
        ],
        default=_route("generic"),
    )
    assert table.lookup("database/saturation", Severity.P1).id == "db-sat-first"
    assert table.lookup("database/saturation", Severity.P2).id == "db-sat-first"


def test_routing_default_when_no_match() -> None:
    # ARCH §4: a routing miss falls through to the default route (generic) —
    # never an exception.
    table = RoutingTable(
        rules=[
            RoutingRule(
                match_classification="database/saturation",
                severities=[Severity.P1],
                route=_route("db-sat"),
            )
        ],
        default=_route("generic"),
    )
    assert table.lookup("network/dns", Severity.P3).id == "generic"
    assert table.lookup("database/saturation", Severity.P4).id == "generic"
