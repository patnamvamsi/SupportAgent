"""WP-215: detectors onboarding proposes from the system's own signals (ADR-0022 §3).

Canonical test IDs from 12-wp WP-215: ``test_detector_rates_measured_not_guessed``,
``test_detector_needs_review_before_enable``, ``test_detector_rule_files_valid``.
(``test_detector_noise_reported`` lands with the stats panel.)
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml

from supportagent.adapters.files.signal_rules import (
    LOKI_RULES,
    PROMETHEUS_RULES,
    REVIEW_FILE,
    read_drafts,
    read_enabled,
    write_review,
    write_rules,
)
from supportagent.adapters.tools.signal_history import HttpSignalHistory
from supportagent.bootstrap import (
    ONBOARDING_DIR,
    _UnrecordedProvider,
    apply_detectors,
    onboard_detectors,
)
from supportagent.domain.signal_detectors import (
    MIN_HISTORY_STEPS,
    STEP_S,
    Signals,
    draft_detectors,
    longest_quiet_s,
    with_enabled,
)
from tests.unit.test_triage_service import _shadow_config

pytestmark = pytest.mark.unit

_DAY = 24 * 3600 // STEP_S


def _busy(steps: int, *, quiet_runs: Iterable[tuple[int, int]] = ()) -> list[float]:
    """``steps`` of activity, with ``(start, length)`` quiet runs."""
    values = [5.0] * steps
    for start, length in quiet_runs:
        for i in range(start, min(steps, start + length)):
            values[i] = 0.0
    return values


def test_longest_quiet_run() -> None:
    assert longest_quiet_s([1, 0, 0, 1, 0, 0, 0, 1]) == 3 * STEP_S
    assert longest_quiet_s([1, 2, 3]) == 0


def test_detector_rates_measured_not_guessed() -> None:
    signals = Signals(
        log_volume={
            # quiet for 30 minutes, regularly: that is its normal
            "feed": _busy(4 * _DAY, quiet_runs=[(i * 50, 6) for i in range(20)]),
            "quiet-job": _busy(4 * _DAY, quiet_runs=[(10, 90)]),  # 7.5 h quiet: normal for it
            "new-svc": _busy(_DAY),  # one day of history
            "silent": [0.0] * (4 * _DAY),
        },
        topic_progress={"PRICES": _busy(4 * _DAY, quiet_runs=[(50, 2)])},
    )
    drafts, skipped = draft_detectors(["feed", "quiet-job", "new-svc", "silent"], signals)
    by_id = {d.id: d for d in drafts}
    feed = by_id["log-silence/feed"]
    assert feed.measured["longest_normal_quiet_min"] == 30
    assert feed.measured["threshold_min"] == 60, "twice the longest quiet run"
    assert feed.expr == 'absent_over_time({service="feed"}[60m])'
    prices = by_id["progress-stopped/prices"]
    assert prices.measured["threshold_min"] == 20, "twice 10 min, above the 10 min floor"
    reasons = {(n.subject, n.reason.split(":")[0].split(";")[0]) for n in skipped}
    assert ("new-svc", "only 24 h of history") in reasons
    assert ("silent", "no activity in the measured history") in reasons
    assert any(s == "quiet-job" and r.startswith("normally quiet") for s, r in reasons)
    assert len(signals.log_volume["new-svc"]) < MIN_HISTORY_STEPS


def test_a_rare_long_quiet_run_is_a_suspected_outage_not_the_threshold() -> None:
    signals = Signals(
        log_volume={"feed": _busy(4 * _DAY, quiet_runs=[(300, 36)])},  # one 3 h gap
        start_ts=1_700_000_000,
    )
    (silence,), _ = draft_detectors(["feed"], signals)
    assert silence.measured["threshold_min"] == 15, "the gap doesn't set the threshold"
    assert silence.measured["suspected_outages"] == ["2023-11-15T23:13Z for 180 min"]


def test_a_recurring_quiet_run_is_normal_not_an_outage() -> None:
    hourly = [(i * 12, 11) for i in range(75)]  # quiet 55 min every hour: a schedule
    signals = Signals(log_volume={"cache": _busy(4 * _DAY, quiet_runs=[*hourly, (1000, 36)])})
    (silence,), _ = draft_detectors(["cache"], signals)
    assert silence.measured["threshold_min"] == 110, "twice the scheduled quiet"
    assert silence.measured["suspected_outages"] == ["step 1000 for 180 min"], (
        "only the run the detector would have caught"
    )


def test_unknown_steps_are_neither_quiet_nor_history() -> None:
    gap: list[float | None] = [None] * (5 * _DAY)
    progress: list[float | None] = [*gap, *_busy(_DAY)]  # an exporter installed a day ago
    drafts, skipped = draft_detectors([], Signals(topic_progress={"ORDERS": progress}))
    assert drafts == []
    assert skipped[0].reason.startswith("only 24 h of history")
    assert longest_quiet_s([1.0, None, 0.0, None, 0.0, 0.0]) == 2 * STEP_S


def test_short_quiet_runs_get_the_floor() -> None:
    drafts, _ = draft_detectors(["api"], Signals(log_volume={"api": _busy(4 * _DAY)}))
    (silence,) = drafts
    assert silence.measured["threshold_min"] == 15, "never quieter than 15 minutes"


def test_binary_classes_need_containers_or_the_host() -> None:
    drafts, skipped = draft_detectors(
        ["api", "worker", 'bad"name'],
        Signals(containers=frozenset({"api", 'bad"name'}), host=True),
    )
    assert {d.id for d in drafts} == {
        "container-down/api",
        "crash-loop/api",
        "host-restart/host",
    }, "no container metrics for worker; an unsafe name is never put in a rule"
    assert skipped == []


def test_detector_needs_review_before_enable(tmp_path: Path) -> None:
    signals = Signals(containers=frozenset({"api"}), log_volume={"api": _busy(4 * _DAY)})
    drafts, skipped = draft_detectors(["api"], signals)
    review = write_review(tmp_path, drafts, skipped, system="sys")
    assert review.name == REVIEW_FILE
    doc = yaml.safe_load(review.read_text())
    assert all(d["enabled"] is False for d in doc["detectors"])
    rules = tmp_path / "rules"
    write_rules(rules, with_enabled(read_drafts(tmp_path), read_enabled(tmp_path)))
    assert yaml.safe_load((rules / PROMETHEUS_RULES).read_text())["groups"][0]["rules"] == []

    for item in doc["detectors"]:  # a person enables one, and tries to edit another's rule
        if item["id"] == "container-down/api":
            item["enabled"] = True
        if item["id"] == "crash-loop/api":
            item["expr"] = "vector(1)"
    review.write_text(yaml.safe_dump(doc))
    enabled, _ = apply_detectors(tmp_path, rules)
    assert [d.id for d in enabled] == ["container-down/api"]
    prom = yaml.safe_load((rules / PROMETHEUS_RULES).read_text())["groups"][0]["rules"]
    assert [r["labels"]["detector"] for r in prom] == ["container-down/api"]
    assert "vector(1)" not in (rules / PROMETHEUS_RULES).read_text(), "only `enabled` is read"

    write_review(tmp_path, drafts, skipped, system="sys")  # re-proposing keeps the review
    assert read_enabled(tmp_path) == {"container-down/api"}


def test_detector_rule_files_valid(tmp_path: Path) -> None:
    signals = Signals(
        containers=frozenset({"api"}),
        host=True,
        log_volume={"api": _busy(4 * _DAY)},
        topic_progress={"ORDERS": _busy(4 * _DAY)},
    )
    drafts, _ = draft_detectors(["api"], signals)
    write_rules(tmp_path, with_enabled(drafts, frozenset(d.id for d in drafts)))
    for name, engine_alerts in (
        (PROMETHEUS_RULES, {"ContainerDown", "CrashLoop", "HostRestart", "ProgressStopped"}),
        (LOKI_RULES, {"LogSilence"}),
    ):
        doc = yaml.safe_load((tmp_path / name).read_text())
        (group,) = doc["groups"]
        assert group["name"] == "supportagent-detectors"
        assert {r["alert"] for r in group["rules"]} == engine_alerts
        for rule in group["rules"]:
            assert rule["expr"] and rule["labels"]["severity"] in {"critical", "warning"}
            assert rule["labels"]["detector"] and rule["annotations"]["summary"]
            assert "service" in rule["labels"] or "topic" in rule["labels"]


def _prom_and_loki(now: datetime) -> tuple[httpx.Client, httpx.Client]:
    start = (int(now.timestamp()) - 4 * 24 * 3600) // STEP_S * STEP_S

    def prom(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if request.url.path == "/api/v1/query" and "container_last_seen" in query:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [
                            {"metric": {"service": "api"}, "value": [0, "1"]},
                            {"metric": {"service": "worker"}, "value": [0, "1"]},
                        ]
                    },
                },
            )
        if request.url.path == "/api/v1/query":
            return httpx.Response(
                200,
                json={"status": "success", "data": {"result": [{"metric": {}, "value": [0, "1"]}]}},
            )
        values = [[start + i * STEP_S, "3"] for i in range(0, 4 * _DAY, 2)]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {"metric": {"topic": "ORDERS"}, "values": values},
                        {"metric": {"topic": "__consumer_offsets"}, "values": values},
                    ]
                },
            },
        )

    def loki(request: httpx.Request) -> httpx.Response:
        if 'service="api"' not in request.url.params["query"]:
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        values = [[start + i * STEP_S, "10"] for i in range(4 * _DAY)]
        return httpx.Response(
            200, json={"status": "success", "data": {"result": [{"metric": {}, "values": values}]}}
        )

    return (
        httpx.Client(base_url="http://prom", transport=httpx.MockTransport(prom)),
        httpx.Client(base_url="http://loki", transport=httpx.MockTransport(loki)),
    )


def test_history_reads_the_deployment_signals() -> None:
    now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    prom, loki = _prom_and_loki(now)
    signals = HttpSignalHistory(prometheus=prom, loki=loki).measure(
        ["api", "worker"], now=now, days=4
    )
    assert signals.containers == {"api", "worker"}
    assert signals.host is True
    assert set(signals.topic_progress) == {"ORDERS"}, "internal topics are skipped"
    orders = signals.topic_progress["ORDERS"]
    assert orders[0] == 3.0 and orders[1] is None, "a step with no sample is unknown"
    assert set(signals.log_volume) == {"api"}, "no stream: the worker doesn't log here"
    assert all(v == 10.0 for v in signals.log_volume["api"][:-1])
    assert signals.start_ts > 0


def test_shared_silence_is_a_collection_gap() -> None:
    from supportagent.adapters.tools.signal_history import _unknown_when_all_silent  # noqa: PLC0415

    volume: dict[str, list[float | None]] = {
        "a": [1.0, 0.0, 0.0],
        "b": [2.0, 0.0, 3.0],
        "c": [1.0, 0.0, 0.0],
    }
    _unknown_when_all_silent(volume)
    assert volume["a"] == [1.0, None, 0.0], "all silent at step 1: collection was down"


class _History:
    def __init__(self, signals: Signals) -> None:
        self.signals = signals
        self.asked: list[str] = []

    def measure(self, components: Iterable[str], *, now: datetime, days: int) -> Signals:
        self.asked = sorted(components)
        return self.signals


def test_onboard_detectors_reviews_the_graph_components(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    shutil.copytree(ONBOARDING_DIR / "system-a", workspace)
    history = _History(
        Signals(
            containers=frozenset({"payment-service"}),
            log_volume={"orders-service": _busy(4 * _DAY)},
        )
    )
    drafts, skipped, path = onboard_detectors(
        _shadow_config(),
        workspace,
        history=history,
        now=datetime(2026, 10, 20, tzinfo=UTC),
        provider=_UnrecordedProvider(),
    )
    assert {"payment-service", "orders-service"} <= set(history.asked), "the graph's components"
    assert {d.id for d in drafts} == {
        "container-down/payment-service",
        "crash-loop/payment-service",
        "log-silence/orders-service",
    }
    assert path.name == REVIEW_FILE and skipped == []
    assert json.loads((workspace / "detectors.json").read_text())[0]["enabled"] is False
