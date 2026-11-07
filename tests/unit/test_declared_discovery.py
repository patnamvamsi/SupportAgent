"""ADR-0016 follow-up: a plugin declares how its live results become graph facts.

The core used to map each live capability (``kafka.topics``, ``k8s.workloads``,
...) in code. Each plugin now declares that mapping as data; the reference
systems' facts are unchanged (the onboarding lab's graph stage), and a stack
the core has never heard of gets a live layer from its plugin alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.bootstrap import discovery_rules, load_plugins
from supportagent.domain.graph import EdgeFact, NodeFact, build
from supportagent.domain.model.plugin import Discovery, DiscoveryRule

pytestmark = pytest.mark.unit

_REDIS = Discovery.model_validate(
    {
        "rules": [
            {"node": {"id": "runtime:redis/{target}", "kind": "Runtime", "tech": ["redis"]}},
            {
                "each": {"db": "result.keyspaces"},
                "node": {"id": "datastore:{target}.{db.key|lower}", "kind": "Datastore"},
            },
            {
                "each": {"db": "result.keyspaces"},
                "edge": ["datastore:{target}.{db.key|lower}", "part_of", "runtime:redis/{target}"],
            },
            {
                "each": {"c": "result.clients", "db": "c.dbs"},
                "edge": ["*:{c.name|strip_prefix:svc-}", "uses", "datastore:{target}.{db|lower}"],
            },
            {
                "each": {"m": "result.modules"},
                "node": {"id": "runtime:redis-module/{m|after_last:/}", "kind": "Runtime"},
            },
        ]
    }
)
_LIVE = {
    "redis.info@cache-eu1": {
        "keyspaces": {"DB0": {"keys": 10}, "DB1": {"keys": 2}},
        "clients": [
            {"name": "svc-orders-api", "dbs": ["DB0"]},
            {"name": "svc-carts", "dbs": ["DB0", "DB1"]},
            {"dbs": ["DB1"]},  # no name: that client yields nothing, the rest still do
        ],
        "modules": ["vendor/search", "vendor/json"],
    },
    "unmapped.thing@x": {"a": 1},  # no plugin maps it: it adds nothing
}


def _facts() -> tuple[set[tuple[str, str, tuple[str, ...]]], set[tuple[str, str, str]]]:
    facts = structured_facts({}, {}, _LIVE, (), [("redis", "redis.info", _REDIS)])
    nodes = {(f.id, f.kind, tuple(f.tech)) for f in facts if isinstance(f, NodeFact)}
    edges = {(f.src, f.kind, f.dst) for f in facts if isinstance(f, EdgeFact)}
    assert all(f.layer == 2 and f.extractor == "live_redis" for f in facts)  # type: ignore[union-attr]
    return nodes, edges


def test_a_stack_the_core_never_heard_of_gets_its_live_layer_from_its_plugin() -> None:
    nodes, edges = _facts()
    assert nodes == {
        ("runtime:redis/cache-eu1", "Runtime", ("redis",)),
        ("datastore:cache-eu1.db0", "Datastore", ()),
        ("datastore:cache-eu1.db1", "Datastore", ()),
        ("runtime:redis-module/search", "Runtime", ()),
        ("runtime:redis-module/json", "Runtime", ()),
    }
    # the clients' placeholders resolve to nothing in a graph with no services: only containment
    assert edges == {
        ("datastore:cache-eu1.db0", "part_of", "runtime:redis/cache-eu1"),
        ("datastore:cache-eu1.db1", "part_of", "runtime:redis/cache-eu1"),
    }


def test_placeholders_resolve_to_the_services_the_graph_knows() -> None:
    services = Discovery.model_validate(
        {
            "rules": [
                {
                    "each": {"s": "result.services"},
                    "node": {"id": "service:{s}", "kind": "Service"},
                }
            ]
        }
    )
    live = {**_LIVE, "toy.services": {"services": ["orders-api", "carts"]}}
    facts = structured_facts(
        {}, {}, live, (), [("redis", "redis.info", _REDIS), ("toy", "toy.services", services)]
    )
    uses = sorted((a, b) for a, rel, b in build(facts).edges if rel == "uses")
    assert uses == [
        ("service:carts", "datastore:cache-eu1.db0"),
        ("service:carts", "datastore:cache-eu1.db1"),
        ("service:orders-api", "datastore:cache-eu1.db0"),
    ]


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ({"node": {"id": "x:{nope}", "kind": "Topic"}}, "reads nothing bound"),
        ({"each": {"t": "g.topics"}, "node": {"id": "x:{t}", "kind": "Topic"}}, "earlier name"),
        ({"each": {"target": "result.x"}, "node": {"id": "x:{target}", "kind": "Topic"}}, "bound"),
        ({"edge": ["a:{target}", "owns", "b:{target}"]}, "is not one of"),
        ({"node": {"id": "x:{target|upper}", "kind": "Topic"}}, "unknown filter"),
        ({"node": {"id": "x:{target|strip_prefix}", "kind": "Topic"}}, "unknown filter"),
        ({"node": {"id": "x:{target}", "kind": "Topic"}, "edge": ["a", "uses", "b"]}, "or an edge"),
        ({}, "or an edge"),
        ({"edge": ["a", "uses"]}, "at least 3"),
    ],
)
def test_a_rule_that_can_not_work_is_refused_when_the_manifest_loads(
    rule: dict[str, object], why: str
) -> None:
    with pytest.raises(ValidationError, match=why):
        DiscoveryRule.model_validate(rule)


def test_the_core_names_no_live_capability() -> None:
    """The mappings live in plugins now: the extractor has no per-capability branch."""
    source = (
        Path(__file__).resolve().parents[2] / "src/supportagent/adapters/files/graph_extract.py"
    ).read_text(encoding="utf-8")
    for capability in (
        "k8s.workloads",
        "oracle.schemas",
        "kafka.topics",
        "kafka.consumer_groups",
        "snowflake.objects",
        "airflow.dags",
    ):
        assert f'"{capability}"' not in source, capability
    owners = {cap: plugin for plugin, cap, _ in discovery_rules(load_plugins())}
    assert owners == {
        "k8s.workloads": "kubernetes",
        "oracle.schemas": "oracle",
        "kafka.topics": "kafka",
        "kafka.consumer_groups": "kafka",
        "snowflake.objects": "snowflake",
        "airflow.dags": "airflow",
    }
