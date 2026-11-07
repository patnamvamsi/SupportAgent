"""ADR-0021 §2: onboarding proposes candidate flows, and drafts one for review.

Candidates come from what tickets ask about by id; ticket numbers and
repeated error codes aren't business objects. A draft orders the services the
tickets name by how data moves through the graph, and is never reviewed.
"""

from __future__ import annotations

import pytest

from supportagent.domain.flow_candidates import Ticket, candidates, data_order, draft_flow
from supportagent.domain.graph import Edge, Node, SystemGraph
from supportagent.domain.model.flow import Flow

pytestmark = pytest.mark.unit


def _graph() -> SystemGraph:
    g = SystemGraph()
    for sid, ci in [
        ("service:shipments-api", "ci-shp"),
        ("service:carrier-adapter", "ci-car"),
        ("service:tracking-worker", "ci-trk"),
        ("service:notifications", "ci-ntf"),
    ]:
        g.nodes[sid] = Node(sid, "Service", sources={f"cmdb:{ci}"})
    g.nodes["topic:shipment-events"] = Node("topic:shipment-events", "Topic")
    for src, kind, dst in [
        ("service:shipments-api", "calls", "service:carrier-adapter"),
        ("service:shipments-api", "publishes", "topic:shipment-events"),
        ("service:tracking-worker", "consumes", "topic:shipment-events"),
        ("service:notifications", "consumes", "topic:shipment-events"),
        ("service:tracking-worker", "calls", "service:notifications"),
    ]:
        edge = Edge(src, kind, dst)
        g.edges[edge.key] = edge
    return g


TICKETS = [
    Ticket("INC0001", "Shipment SHP-100001 not delivered, customer chasing", "ci-trk"),
    Ticket("INC0002", "where is shipment SHP-100002? no tracking updates", "ci-trk"),
    Ticket("INC0003", "customer never got the SMS for shipment SHP-100003", "ci-ntf"),
    Ticket("INC0004", "ORA-01653 unable to extend table in shipmentsdb", "ci-shp"),
    Ticket("INC0005", "ORA-01653 again on the nightly load", "ci-shp"),
    Ticket("INC0006", "Duplicate of INC0001", "ci-trk"),
    Ticket("INC0007", "booking failed for shipment SHP-100004 at the carrier", "ci-car"),
]


def test_candidates_rank_what_tickets_ask_about() -> None:
    got = candidates(TICKETS, _graph())
    assert [c.pattern for c in got] == [r"SHP\-\d{6}"]
    top = got[0]
    assert (top.noun, top.tickets) == ("shipment", 4)
    assert top.examples == ("SHP-100001", "SHP-100002", "SHP-100003")
    assert top.services[0] == "service:tracking-worker"
    assert set(top.services) == {
        "service:tracking-worker",
        "service:notifications",
        "service:carrier-adapter",
    }


def test_no_business_objects_means_no_candidates() -> None:
    infra = [Ticket(f"INC000{i}", "disk full on db host", "ci-shp") for i in range(5)]
    assert candidates(infra, _graph()) == []


def test_data_order_follows_calls_and_topics() -> None:
    order = data_order(
        _graph(),
        [
            "service:notifications",
            "service:tracking-worker",
            "service:carrier-adapter",
            "service:shipments-api",
        ],
    )
    assert order.index("service:shipments-api") < order.index("service:carrier-adapter")
    assert order.index("service:shipments-api") < order.index("service:tracking-worker")
    assert order.index("service:tracking-worker") < order.index("service:notifications")


def test_draft_is_a_valid_unreviewed_flow() -> None:
    top = candidates(TICKETS, _graph())[0]
    draft = draft_flow(top, _graph(), flow_id="shipment")
    flow = Flow.model_validate(draft)
    assert not flow.reviewed
    assert flow.accepts("SHP-100001") and not flow.accepts("SHP-1")
    assert [s.component for s in flow.stages] == [
        "service:carrier-adapter",
        "service:tracking-worker",
        "service:notifications",
    ]
    assert flow.stages[-1].terminal
    assert all(s.within_min is None for s in flow.stages)  # a person sets the limits
    assert flow.stages[0].lookup.refs() == {"object.id"}
