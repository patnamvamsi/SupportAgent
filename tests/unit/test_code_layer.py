"""WP-221: the code layer of the system graph (ADR-0023 §3).

Canonical test IDs from 12-wp WP-221: ``test_code_facts_cite_file_and_line``,
``test_topic_constant_resolved_across_modules``, ``test_fstring_topic_becomes_pattern``,
``test_component_owns_its_build_context``. (``test_defined_not_running_marked`` and
``test_live_signal_marks_fact_observed`` land with live corroboration.)

The system here is synthetic: two Compose services built from one context, a
shared library, a Kafka feed and its consumer, a bar store written and read
through f-string SQL. The rules are the shipped plugins' (plugins as data).
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from supportagent.adapters.files.code_python import python_findings
from supportagent.adapters.files.graph_extract import structured_facts
from supportagent.bootstrap import load_plugins
from supportagent.domain.graph import EdgeFact, SystemGraph, build
from supportagent.domain.model.plugin import CodeRule, Extractor, NodeTemplate

pytestmark = pytest.mark.unit

_COMPOSE = """
services:
  feed:
    build: {context: ., dockerfile: feed/Dockerfile}
    depends_on: [broker]
  trader:
    build: {context: ., dockerfile: trader/Dockerfile}
  trader_worker:
    build: {context: ., dockerfile: trader/Dockerfile}
  broker:
    image: kafka:3
"""
_OTHER_COMPOSE = """
services:
  trader-worker:
    build: ../trader
"""
_FEED_DOCKERFILE = "FROM python:3.12\nCOPY shared /shared\nCOPY feed /app\n"
_TRADER_DOCKERFILE = (
    "FROM python:3.12 AS base\nCOPY --from=base /x /x\nCOPY trader/requirements.txt /app/\n"
    'COPY ["trader", "/app"]\n'
)
_CONFIG = """
import os
BARS_TOPIC = os.getenv("BARS_TOPIC", "BARS")
NEWS_URL = "https://api.newsvendor.com/v2/latest"
"""
_FEED = '''
"""The feed. SELECT * FROM docstring_table is prose, not SQL."""
import requests
from kafka import KafkaProducer
from apscheduler.schedulers.background import BackgroundScheduler
from feed.config import BARS_TOPIC, NEWS_URL


class Feed:
    def __init__(self):
        self.producer = KafkaProducer(bootstrap_servers="broker:9092")

    def _topic_for(self, symbol):
        return f"ticks.{symbol}"

    def publish(self, symbol, tick):
        topic = self._topic_for(symbol)
        self.producer.send(topic, tick)
        self.producer.send(BARS_TOPIC, tick)

    def store(self, venue, symbol, interval, conn):
        table = f"{venue}_{symbol}_bars_{interval}"
        conn.execute(f"INSERT INTO {table} VALUES (%s)")
        requests.get(NEWS_URL, timeout=5)


def sync_bars():
    pass


scheduler = BackgroundScheduler()
scheduler.add_job(sync_bars, "cron", minute=5)
'''
_SHARED = """
from sqlalchemy.orm import declarative_base
Base = declarative_base()


class Cursor(Base):
    __tablename__ = "runner_cursors"
"""
_TRADER = """
from kafka import KafkaConsumer


class Trader:
    def __init__(self, symbols):
        self.consumer = KafkaConsumer(bootstrap_servers="broker:9092")
        self.consumer.subscribe(list({self._topic(s) for s in symbols}))

    def _topic(self, symbol):
        return f"ticks.{symbol}"

    def bars(self, conn, venue, symbol):
        table = f"{venue}_{symbol}_bars_1m"
        return conn.execute(f"SELECT close FROM {table} ORDER BY ts")
"""


def _system() -> dict[str, str]:
    return {
        "platform/docker-compose.yml": _COMPOSE,
        "platform/trader/docker-compose.dev.yml": _OTHER_COMPOSE,
        "platform/feed/Dockerfile": _FEED_DOCKERFILE,
        "platform/trader/Dockerfile": _TRADER_DOCKERFILE,
        "platform/feed/config.py": _CONFIG,
        "platform/feed/app.py": _FEED,
        "platform/shared/models.py": _SHARED,
        "platform/trader/app.py": _TRADER,
        "platform/trader/broken.py": "print 'python 2'\n",
    }


def _declared() -> list[tuple[str, Extractor]]:
    return [(p.manifest.id, e) for p in load_plugins() for e in p.manifest.provides.extractors]


def _graph(files: Mapping[str, str]) -> SystemGraph:
    return build(structured_facts(files, {}, {}, _declared()))


def _edges(graph: SystemGraph) -> set[tuple[str, str, str]]:
    return set(graph.edges)


def test_code_facts_cite_file_and_line() -> None:
    facts = structured_facts(_system(), {}, {}, _declared())
    sources = {
        (f.src, f.kind, f.dst): f.source
        for f in facts
        if isinstance(f, EdgeFact) and f.extractor.startswith("plugin:python-runtime/")
    }
    lines = _FEED.splitlines()
    send = 1 + next(i for i, t in enumerate(lines) if "send(topic" in t)
    job = 1 + next(i for i, t in enumerate(lines) if "add_job(" in t)
    assert sources[("service:feed", "publishes", "topic:ticks.*")] == (
        f"git:platform/feed/app.py:{send}"
    )
    assert sources[("job:feed.sync_bars", "part_of", "service:feed")] == (
        f"git:platform/feed/app.py:{job}"
    )


def test_topic_constant_resolved_across_modules() -> None:
    edges = _edges(_graph(_system()))
    assert ("service:feed", "publishes", "topic:bars") in edges, "os.getenv default, imported"
    assert ("service:feed", "calls", "external:newsvendor") in edges, "a URL constant"


def test_fstring_topic_becomes_pattern() -> None:
    edges = _edges(_graph(_system()))
    assert ("service:feed", "publishes", "topic:ticks.*") in edges
    assert ("service:trader", "consumes", "topic:ticks.*") in edges, "a comprehension's items"
    # f-string SQL: the writer's and the reader's patterns are one table
    assert ("service:feed", "writes", "table:*_*_bars_*") in edges
    assert ("service:trader", "reads", "table:*_*_bars_*") in edges, "*_*_bars_1m is covered"


def test_component_owns_its_build_context() -> None:
    edges = _edges(_graph(_system()))
    # shared/ is copied into feed's image, not trader's
    assert ("service:feed", "uses", "table:runner_cursors") in edges
    assert ("service:trader", "uses", "table:runner_cursors") not in edges
    # trader/ belongs to both services built from it, and to no other
    assert ("service:trader-worker", "consumes", "topic:ticks.*") in edges
    assert ("service:feed", "consumes", "topic:ticks.*") not in edges
    nodes = set(_graph(_system()).nodes)
    assert "service:trader_worker" not in nodes, "trader_worker and trader-worker are one"


def test_prose_and_unparseable_code_read_nothing() -> None:
    rules = [(pid, r) for pid, e in _declared() if e.format == "python" for r in e.code]
    report = python_findings(_system(), rules)
    assert report.unparsed == ["platform/trader/broken.py"]
    assert not any("docstring_table" in f.value for f in report.findings)


def test_code_rules_are_validated() -> None:
    topic = NodeTemplate(id="topic:{value}", kind="Topic")
    with pytest.raises(ValueError, match="names the argument"):
        CodeRule(id="no-arg", match="call", name="send", node=topic, edge="publishes")
    with pytest.raises(ValueError, match="receiver applies"):
        CodeRule(id="bad", match="assign", receiver="x", node=topic, edge="uses")
    with pytest.raises(ValueError, match="names a group"):
        CodeRule(
            id="grp",
            match="assign",
            node=NodeTemplate(id="topic:{missing}", kind="Topic"),
            edge="uses",
        )
    with pytest.raises(ValueError, match="format: python"):
        Extractor(match="**/*.py", format="python")
    with pytest.raises(ValueError, match="format: python"):
        Extractor(
            match="**/*.py",
            code=[CodeRule(id="ok", match="assign", node=topic, edge="uses")],
        )
