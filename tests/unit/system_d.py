"""System D's monitoring, simulated: ``evals/live/system-d/signals.yaml`` served as the
Prometheus and Loki HTTP APIs (WP-211, WP-226).

Onboarding measures it and the detector evaluator queries it through the real
``HttpSignalHistory``, so the run in CI reads a deployment the way an instance does.
Only the queries SupportAgent sends are understood; any other is a test failure, so a
change in what is queried shows up here instead of passing against a guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import yaml

ROOT = Path("evals/live/system-d")
_NS = 1_000_000_000


@dataclass(slots=True)
class _Override:
    start: int
    end: int | None
    logs: dict[str, float] = field(default_factory=dict)
    topics: dict[str, float] = field(default_factory=dict)
    down: frozenset[str] = frozenset()

    def covers(self, ts: int) -> bool:
        return self.start <= ts and (self.end is None or ts < self.end)


def _minute_of_day(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


class SystemD:
    def __init__(self, now: Callable[[], datetime], root: Path = ROOT) -> None:
        self.spec: dict[str, Any] = yaml.safe_load((root / "signals.yaml").read_text())
        self.truth: dict[str, Any] = yaml.safe_load((root / "truth.yaml").read_text())
        self.step_s = int(self.spec["step_s"])
        self._now = now
        onboarded = int(now().timestamp())
        self._overrides = [
            _Override(
                start=onboarded + int(h["from_h"] * 3600),
                end=onboarded + int(h["from_h"] * 3600) + int(h["minutes"]) * 60,
                logs={k: float(v) for k, v in (h.get("logs") or {}).items()},
                topics={k: float(v) for k, v in (h.get("topics") or {}).items()},
            )
            for h in self.spec.get("history", [])
        ]
        self.asked: list[str] = []

    # --- what happens ------------------------------------------------------------------

    def start(self, scenario: str) -> None:
        spec = self.spec["scenarios"][scenario]
        self._overrides.append(
            _Override(
                start=self._ts(),
                end=None,
                topics={k: float(v) for k, v in (spec.get("topics") or {}).items()},
                down=frozenset(spec.get("down") or ()),
            )
        )

    def stop(self) -> None:
        """Every running scenario recovers now."""
        now = self._ts()
        for o in self._overrides:
            if o.end is None:
                o.end = now

    def _ts(self) -> int:
        return int(self._now().timestamp())

    def _active(self, ts: int) -> list[_Override]:
        return [o for o in self._overrides if o.covers(ts)]

    def running(self, service: str, ts: int) -> bool:
        spec = self.spec["services"].get(service)
        if spec is None or spec.get("defined_only"):
            return False
        return not any(service in o.down for o in self._active(ts))

    def _pattern(self, spec: dict[str, Any], ts: int) -> float:
        if spec.get("never"):
            return 0.0
        quiet = spec.get("quiet")
        if quiet:
            minute = (ts % 86400) // 60
            if _minute_of_day(quiet[0]) <= minute < _minute_of_day(quiet[1]):
                return 0.0
        return 10.0 if (ts // self.step_s) % int(spec.get("every", 1)) == 0 else 0.0

    def lines(self, service: str, ts: int) -> float | None:
        """Log lines in the step starting at ``ts`` (``None``: the service has no stream)."""
        spec = self.spec["services"].get(service) or {}
        if "logs" not in spec:
            return None
        if not self.running(service, ts):
            return 0.0
        for o in self._active(ts):
            if service in o.logs:
                return o.logs[service]
        return self._pattern(spec["logs"], ts)

    def published(self, topic: str, ts: int) -> float:
        for o in self._active(ts):
            if topic in o.topics:
                return o.topics[topic]
        return self._pattern(self.spec["topics"][topic], ts)

    def _window(self, fn: Callable[[int], float | None], minutes: int) -> float:
        """The total over the steps that start in the last ``minutes``."""
        now = self._ts()
        first = -(-(now - minutes * 60) // self.step_s) * self.step_s
        return sum(fn(t) or 0.0 for t in range(first, now, self.step_s))

    # --- the HTTP APIs -----------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        query = params["query"]
        self.asked.append(query)
        path = request.url.path
        if path.endswith("/query_range"):
            loki = path.startswith("/loki/")
            start, end = int(params["start"]), int(params["end"])
            if loki:
                start, end = start // _NS, end // _NS
            result = self._range(query, start, end, int(params["step"]), loki=loki)
            return _ok({"resultType": "matrix", "result": result})
        if path.endswith("/query"):
            return _ok({"resultType": "vector", "result": self._instant(query)})
        raise AssertionError(f"System D serves no {path}")  # noqa: TRY003

    def _range(self, query: str, start: int, end: int, step: int, *, loki: bool) -> list[Any]:
        points = range(start, end + 1, step)
        if (
            not loki
            and query == f"sum by (topic) (increase(kafka_topic_partition_current_offset[{step}s]))"
        ):
            return [
                {"metric": {"topic": t}, "values": [[p, str(self.published(t, p))] for p in points]}
                for t in self.spec["topics"]
            ]
        if loki and (
            m := re.fullmatch(
                rf'sum\(count_over_time\(\{{service="([^"]+)"\}}\[{step}s\]\)\)', query
            )
        ):
            service = m.group(1)
            if self.lines(service, start) is None:
                return []
            return [{"metric": {}, "values": [[p, str(self.lines(service, p))] for p in points]}]
        raise AssertionError(f"System D can't answer the range query {query!r}")  # noqa: TRY003

    def _instant(self, query: str) -> list[Any]:  # noqa: PLR0911 — one case per query shape
        now = self._ts()
        services = self.spec["services"]
        if query == 'count by (service) (container_last_seen{service!=""})':
            return [_series({"service": s}) for s in services if self.running(s, now)]
        if query == "count(node_boot_time_seconds)":
            return [_series({})] if self.spec.get("host") else []
        if query == "changes(node_boot_time_seconds[30m]) > 0":
            return []
        if m := re.fullmatch(
            r'time\(\) - max_over_time\(container_last_seen\{service="([^"]+)"\}\[1h\]\) > 120',
            query,
        ):
            # Down for more than 120 s, and seen within the hour (else there is no series).
            service = m.group(1)
            seen = [t for t in range(now - 3600, now + 1, 60) if self.running(service, t)]
            return [_series({"service": service})] if seen and now - seen[-1] > 120 else []
        if re.fullmatch(
            r'changes\(container_start_time_seconds\{service="[^"]+"\}\[30m\]\) >= 3', query
        ):
            return []
        if m := re.fullmatch(r'absent_over_time\(\{service="([^"]+)"\}\[(\d+)m\]\)', query):
            service, minutes = m.group(1), int(m.group(2))
            silent = self._window(lambda t: self.lines(service, t), minutes) == 0
            return [_series({"service": service})] if silent else []
        if m := re.fullmatch(
            r"sum\(increase\(kafka_topic_partition_current_offset"
            r'\{topic="([^"]+)"\}\[(\d+)m\]\)\) == 0',
            query,
        ):
            topic, minutes = m.group(1), int(m.group(2))
            idle = self._window(lambda t: self.published(topic, t), minutes) == 0
            return [_series({})] if idle else []
        raise AssertionError(f"System D can't answer the query {query!r}")  # noqa: TRY003


def _series(labels: dict[str, str]) -> dict[str, Any]:
    return {"metric": labels, "value": [0, "1"]}


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content=json.dumps({"status": "success", "data": data}))


__all__ = ["ROOT", "SystemD"]
