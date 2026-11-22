"""What a deployment measures, read from its Prometheus and Loki (WP-215).

Onboarding asks this before it proposes detectors (``domain.signal_detectors``):
which components have container metrics, whether the host's boot time is
scraped, each Kafka topic's offset progress, and each component's log volume,
over the measured history in fixed steps. Read-only: instant and range queries
only.

A metric step with no sample is unknown (``None``): the exporter didn't exist
yet, or wasn't scraped. Loki returns no point for a step with no lines, so a
log step is zero, unless every component was silent at once: with three or
more components logging, that means collection (or the host) was down, and
the step is unknown for all of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from supportagent.adapters.tools._http import get_json
from supportagent.domain.errors import ToolError
from supportagent.domain.signal_detectors import STEP_S, Engine, Signals

_TIMEOUT_S = 30.0
_NS = 1_000_000_000
#: With at least this many components logging, all silent at once is a gap
#: in collection rather than quiet.
_SHARED_SILENCE_MIN = 3

Series = list[tuple[dict[str, str], list[float | None]]]


@dataclass(frozen=True, slots=True)
class _Grid:
    """The measured history: ``steps`` points, ``step_s`` apart, from ``start``."""

    start: int
    end: int
    step_s: int

    @property
    def steps(self) -> int:
        return (self.end - self.start) // self.step_s + 1


def _labels(item: Mapping[str, object]) -> dict[str, str]:
    metric = item.get("metric")
    return {str(k): str(v) for k, v in metric.items()} if isinstance(metric, dict) else {}


def _results(body: object) -> list[Mapping[str, object]]:
    data = body.get("data") if isinstance(body, dict) else None
    result = data.get("result") if isinstance(data, dict) else None
    return [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []


def _vector(body: object) -> list[tuple[dict[str, str], float]]:
    out = []
    for item in _results(body):
        value = item.get("value")
        try:
            number = float(value[1]) if isinstance(value, list) and len(value) == 2 else None  # noqa: PLR2004
        except (TypeError, ValueError):
            number = None
        if number is not None:
            out.append((_labels(item), number))
    return out


def _matrix(body: object, grid: _Grid) -> Series:
    out = []
    for item in _results(body):
        values: list[float | None] = [None] * grid.steps
        raw = item.get("values")
        for pair in raw if isinstance(raw, list) else []:
            try:
                index = round((float(pair[0]) - grid.start) / grid.step_s)
                value = float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= index < grid.steps:
                values[index] = value
        out.append((_labels(item), values))
    return out


class HttpSignalHistory:
    def __init__(
        self,
        *,
        prometheus: httpx.Client | None,
        loki: httpx.Client | None,
        loki_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._prom = prometheus
        self._loki = loki
        self._loki_headers = dict(loki_headers or {})

    def measure(
        self, components: Iterable[str], *, now: datetime, days: int, step_s: int = STEP_S
    ) -> Signals:
        grid = _Grid(
            start=int((now - timedelta(days=days)).timestamp()) // step_s * step_s,
            end=int(now.timestamp()) // step_s * step_s,
            step_s=step_s,
        )
        containers: frozenset[str] = frozenset()
        host = False
        topics: dict[str, list[float | None]] = {}
        if self._prom is not None:
            containers = frozenset(
                labels["service"]
                for labels, _ in self._instant(
                    self._prom, 'count by (service) (container_last_seen{service!=""})'
                )
                if labels.get("service")
            )
            host = any(v > 0 for _, v in self._instant(self._prom, "count(node_boot_time_seconds)"))
            query = f"sum by (topic) (increase(kafka_topic_partition_current_offset[{step_s}s]))"
            for labels, series in self._range(self._prom, query, grid):
                topic = labels.get("topic", "")
                if topic and not topic.startswith("_"):
                    topics[topic] = series
        volume: dict[str, list[float | None]] = {}
        if self._loki is not None:
            for name in sorted(set(components)):
                found = self._log_volume(self._loki, name, grid)
                if found is not None:
                    volume[name] = found
            _unknown_when_all_silent(volume)
        return Signals(
            containers=containers,
            host=host,
            log_volume=volume,
            topic_progress=topics,
            start_ts=grid.start,
        )

    def instant(self, engine: Engine, expr: str) -> list[dict[str, str]]:
        """WP-224: one detector's query now; each series it returns, by its labels."""
        if engine == "prometheus":
            if self._prom is None:
                raise ToolError("no Prometheus to query: configure tools.prometheus")  # noqa: TRY003
            return [labels for labels, _ in self._instant(self._prom, expr)]
        if self._loki is None:
            raise ToolError("no Loki to query: configure tools.loki")  # noqa: TRY003
        body = get_json(
            self._loki,
            "/loki/api/v1/query",
            params={"query": expr},
            headers=self._loki_headers,
            timeout_s=_TIMEOUT_S,
        )
        return [labels for labels, _ in _vector(body)]

    @staticmethod
    def _instant(client: httpx.Client, query: str) -> list[tuple[dict[str, str], float]]:
        body = get_json(client, "/api/v1/query", params={"query": query}, timeout_s=_TIMEOUT_S)
        return _vector(body)

    @staticmethod
    def _range(client: httpx.Client, query: str, grid: _Grid) -> Series:
        params: dict[str, str | int] = {
            "query": query,
            "start": grid.start,
            "end": grid.end,
            "step": grid.step_s,
        }
        body = get_json(client, "/api/v1/query_range", params=params, timeout_s=_TIMEOUT_S)
        return _matrix(body, grid)

    def _log_volume(self, loki: httpx.Client, name: str, grid: _Grid) -> list[float | None] | None:
        if '"' in name or "\\" in name:
            return None
        params: dict[str, str | int] = {
            "query": f'sum(count_over_time({{service="{name}"}}[{grid.step_s}s]))',
            "start": grid.start * _NS,
            "end": grid.end * _NS,
            "step": grid.step_s,
        }
        body = get_json(
            loki,
            "/loki/api/v1/query_range",
            params=params,
            headers=self._loki_headers,
            timeout_s=_TIMEOUT_S,
        )
        series = _matrix(body, grid)
        if not series:
            return None  # no stream: the component doesn't log here
        columns = zip(*(values for _, values in series), strict=True)
        return [sum(v or 0.0 for v in column) for column in columns]  # no point: no lines


def _unknown_when_all_silent(volume: dict[str, list[float | None]]) -> None:
    if len(volume) < _SHARED_SILENCE_MIN:
        return
    series = list(volume.values())
    for i in range(len(series[0])):
        if all(not (s[i] or 0.0) for s in series):
            for s in series:
                s[i] = None


def signal_history(
    prometheus: httpx.Client | None, loki: httpx.Client | None, loki_headers: Mapping[str, str]
) -> HttpSignalHistory:
    """The history reader, or a ``ToolError`` when the deployment measures nothing."""
    if prometheus is None and loki is None:
        raise ToolError(  # noqa: TRY003
            "no signals to measure: configure tools.prometheus and/or tools.loki (base_url)"
        )
    return HttpSignalHistory(prometheus=prometheus, loki=loki, loki_headers=loki_headers)


__all__ = ["HttpSignalHistory", "signal_history"]
