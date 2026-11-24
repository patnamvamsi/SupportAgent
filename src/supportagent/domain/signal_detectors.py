"""Signal detectors onboarding proposes (ADR-0022 §3, WP-215): reviewed alert rules.

(Not the secret-redaction detectors of ``domain.detectors``.) A system with no
alerting, or alerting that misses its real failures, is the normal case. From
the components onboarding found and the signals the deployment measures, this
drafts one detector per generic failure class:

| Class | Signal | Fires when |
|---|---|---|
| ``container_down`` | cAdvisor ``container_last_seen`` | not seen for 2 minutes |
| ``crash_loop`` | cAdvisor ``container_start_time_seconds`` | 3 starts in 30 minutes |
| ``log_silence`` | Loki line volume | silent longer than its measured normal gap |
| ``progress_stopped`` | Kafka topic offsets | not advancing longer than its measured normal idle |
| ``host_restart`` | node-exporter ``node_boot_time_seconds`` | the host booted |

**Measured, never guessed.** A silence or idle threshold is twice the longest
normal quiet run in the measured history (with a floor). Two things found on
the proving ground shape "normal":

- a step with no sample is *unknown*, not quiet (an exporter that didn't exist
  yet, collection that was down): it never lengthens a quiet run, and only
  steps with data count as history;
- the history can hold the very outages a detector should catch. The longest
  quiet runs, together at most 5% of the history, are set aside as *suspected
  outages* and listed (with when they began) for the reviewer, instead of
  becoming the threshold.

A signal with too little history, or too irregular to tell quiet from broken,
gets no detector: it is listed with the reason instead. The two binary classes
(a container gone, a host rebooted) need no rate.

**Reviewed before enabled.** Every draft starts ``enabled: false`` in the
review file; only the ones a person enables are rendered into rule files, and
onboarding never writes into the alerting system itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Literal

SignalClass = Literal[
    "container_down", "crash_loop", "log_silence", "progress_stopped", "host_restart"
]
Engine = Literal["prometheus", "loki"]

#: History is read in steps of this many seconds.
STEP_S = 300
#: Less history than this (in steps) proposes no rate-based detector.
MIN_HISTORY_STEPS = 3 * 24 * 3600 // STEP_S
#: The quietest a silence threshold may be, and the longest normal quiet run
#: that still leaves a useful detector (beyond it, quiet is the normal state).
MIN_SILENCE_S = 15 * 60
MIN_IDLE_S = 10 * 60
MAX_NORMAL_QUIET_S = 6 * 3600
#: The share of the history the longest quiet runs may cover and still be set
#: aside as suspected outages, and how many of them at most.
OUTLIER_SHARE = 0.05
MAX_OUTLIERS = 5
_SLUG = re.compile(r"[^a-z0-9_.-]+")
_LABEL_VALUE = re.compile(r'^[^"\\\n{}]{1,128}$')


@dataclass(frozen=True, slots=True)
class SignalDetector:
    id: str
    cls: SignalClass
    subject: str
    engine: Engine
    expr: str
    for_s: int
    severity: str
    summary: str
    measured: Mapping[str, object] = field(default_factory=dict)
    enabled: bool = False
    #: The component an alert is about when the subject isn't one (a topic's producer).
    service: str = ""


@dataclass(frozen=True, slots=True)
class NotProposed:
    cls: SignalClass
    subject: str
    reason: str


@dataclass(frozen=True, slots=True)
class Signals:
    """What the deployment measures, as onboarding found it (an adapter fills this)."""

    #: Components with container metrics (the Compose service, as ``service``).
    containers: frozenset[str] = frozenset()
    #: The host's boot time is scraped (node-exporter).
    host: bool = False
    #: Per component: log lines per step over the measured history (None: unknown).
    log_volume: Mapping[str, Sequence[float | None]] = field(default_factory=dict)
    #: Per topic: offset increase per step over the measured history (None: unknown).
    topic_progress: Mapping[str, Sequence[float | None]] = field(default_factory=dict)
    #: The first step's time (epoch seconds), to say when a suspected outage began.
    start_ts: int = 0


def quiet_runs(values: Sequence[float | None]) -> list[tuple[int, int]]:
    """Each maximal run of steps with nothing (``<= 0``), as ``(start, length)``.

    An unknown step (``None``) ends a run and is never counted in one.
    """
    runs: list[tuple[int, int]] = []
    start = -1
    for i, value in enumerate([*values, None]):
        if value is not None and value <= 0:
            if start < 0:
                start = i
            continue
        if start >= 0:
            runs.append((start, i - start))
            start = -1
    return runs


def longest_quiet_s(values: Sequence[float | None], step_s: int = STEP_S) -> int:
    """The longest run of steps with nothing, in seconds."""
    return max((length for _, length in quiet_runs(values)), default=0) * step_s


@dataclass(frozen=True, slots=True)
class _Measured:
    threshold_s: int
    normal_quiet_s: int
    history_h: int
    outages: tuple[tuple[int, int], ...]  # (start step, length in steps)


def _threshold(values: Sequence[float | None], step_s: int, floor_s: int) -> _Measured | str:
    """The measured threshold, or why none can be measured."""
    covered = sum(1 for v in values if v is not None)
    if covered * step_s < MIN_HISTORY_STEPS * STEP_S:
        return f"only {covered * step_s // 3600} h of history; needs 72 h"
    if not any(v is not None and v > 0 for v in values):
        return "no activity in the measured history"
    runs = sorted(quiet_runs(values), key=lambda r: -r[1])
    outages: list[tuple[int, int]] = []
    budget = int(covered * OUTLIER_SHARE)
    while runs and len(outages) < MAX_OUTLIERS and runs[0][1] * step_s > floor_s:
        if sum(n for _, n in outages) + runs[0][1] > budget:
            break
        outages.append(runs.pop(0))
    while True:  # an outage is a run the measured detector would have caught
        quiet = max((n for _, n in runs), default=0) * step_s
        normal = [o for o in outages if o[1] * step_s <= max(2 * quiet, floor_s)]
        if not normal:
            break
        runs += normal  # a quiet run that recurs (a schedule) is normal
        outages = [o for o in outages if o not in normal]
    if quiet > MAX_NORMAL_QUIET_S:
        return f"normally quiet for up to {quiet // 60} min: quiet can't be told from failure"
    return _Measured(
        threshold_s=max(2 * quiet, floor_s),
        normal_quiet_s=quiet,
        history_h=covered * step_s // 3600,
        outages=tuple(sorted(outages)),
    )


def _measured(m: _Measured, start_ts: int, step_s: int, quiet_key: str) -> dict[str, object]:
    out: dict[str, object] = {
        "history_h": m.history_h,
        quiet_key: m.normal_quiet_s // 60,
        "threshold_min": m.threshold_s // 60,
    }
    if m.outages:
        out["suspected_outages"] = [
            (
                datetime.fromtimestamp(start_ts + first * step_s, UTC).strftime("%Y-%m-%dT%H:%MZ")
                if start_ts
                else f"step {first}"
            )
            + f" for {length * step_s // 60} min"
            for first, length in m.outages
        ]
    return out


def _minutes(seconds: int) -> str:
    return f"{max(1, seconds // 60)}m"


def _safe(value: str) -> bool:
    return bool(_LABEL_VALUE.match(value))


def detector_id(cls: SignalClass, subject: str) -> str:
    return f"{cls.replace('_', '-')}/{_SLUG.sub('-', subject.lower()).strip('-')}"


def _container(name: str) -> list[SignalDetector]:
    return [
        SignalDetector(
            id=detector_id("container_down", name),
            cls="container_down",
            subject=name,
            engine="prometheus",
            expr=f'time() - max_over_time(container_last_seen{{service="{name}"}}[1h]) > 120',
            for_s=60,
            severity="critical",
            summary=f"{name}: its container is not running",
            measured={"signal": "container_last_seen", "binary": True},
        ),
        SignalDetector(
            id=detector_id("crash_loop", name),
            cls="crash_loop",
            subject=name,
            engine="prometheus",
            expr=f'changes(container_start_time_seconds{{service="{name}"}}[30m]) >= 3',
            for_s=0,
            severity="critical",
            summary=f"{name}: its container keeps restarting",
            measured={"signal": "container_start_time_seconds", "binary": True},
        ),
    ]


def _silence(
    name: str, volume: Sequence[float | None], step_s: int, start_ts: int
) -> SignalDetector | NotProposed:
    found = _threshold(volume, step_s, MIN_SILENCE_S)
    if isinstance(found, str):
        return NotProposed("log_silence", name, found)
    window = _minutes(found.threshold_s)
    return SignalDetector(
        id=detector_id("log_silence", name),
        cls="log_silence",
        subject=name,
        engine="loki",
        expr=f'absent_over_time({{service="{name}"}}[{window}])',
        for_s=0,
        severity="critical",
        summary=f"{name}: no log lines for {window} (it may be stalled)",
        measured=_measured(found, start_ts, step_s, "longest_normal_quiet_min"),
    )


def _progress(
    topic: str, progress: Sequence[float | None], step_s: int, start_ts: int, producer: str
) -> SignalDetector | NotProposed:
    found = _threshold(progress, step_s, MIN_IDLE_S)
    if isinstance(found, str):
        return NotProposed("progress_stopped", topic, found)
    window = _minutes(found.threshold_s)
    by = f" (published by {producer})" if producer else ""
    return SignalDetector(
        id=detector_id("progress_stopped", topic),
        cls="progress_stopped",
        subject=topic,
        engine="prometheus",
        expr=(
            f'sum(increase(kafka_topic_partition_current_offset{{topic="{topic}"}}[{window}])) == 0'
        ),
        for_s=0,
        severity="critical",
        summary=f"topic {topic}: nothing published for {window}{by}",
        measured=_measured(found, start_ts, step_s, "longest_normal_idle_min"),
        service=producer,
    )


def draft_detectors(
    components: Iterable[str],
    signals: Signals,
    *,
    producers: Mapping[str, Iterable[str]] | None = None,
    step_s: int = STEP_S,
) -> tuple[list[SignalDetector], list[NotProposed]]:
    """One detector per class and component (or topic) the signals support.

    ``producers``: each topic's publishing components, from the graph. A topic's
    detector names its producer, so its alert lands on that service; with several,
    the ones seen running decide, and if that still leaves more than one, none.
    """
    drafts: list[SignalDetector] = []
    skipped: list[NotProposed] = []

    def _producer(topic: str) -> str:
        # The code may name a topic by pattern (an f-string reads as ``ticks.*``).
        known = producers or {}
        found = known.get(topic) or {
            p for pattern, names in known.items() if fnmatchcase(topic, pattern) for p in names
        }
        candidates = sorted(set(found))
        running = [c for c in candidates if c in signals.containers]
        chosen = running if signals.containers else candidates
        return chosen[0] if len(chosen) == 1 and _safe(chosen[0]) else ""

    def add(found: SignalDetector | NotProposed) -> None:
        if isinstance(found, SignalDetector):
            drafts.append(found)
        else:
            skipped.append(found)

    for name in sorted(set(components)):
        if not _safe(name):
            continue
        if name in signals.containers:
            drafts.extend(_container(name))
        volume = signals.log_volume.get(name)
        if volume is not None:
            add(_silence(name, volume, step_s, signals.start_ts))
    for topic, progress in sorted(signals.topic_progress.items()):
        if _safe(topic):
            add(_progress(topic, progress, step_s, signals.start_ts, _producer(topic)))
    if signals.host:
        drafts.append(
            SignalDetector(
                id="host-restart/host",
                cls="host_restart",
                subject="host",
                engine="prometheus",
                expr="changes(node_boot_time_seconds[30m]) > 0",
                for_s=0,
                severity="warning",
                summary="the host rebooted: check that every component came back",
                measured={"signal": "node_boot_time_seconds", "binary": True},
            )
        )
    return drafts, skipped


def alert_rule(det: SignalDetector) -> dict[str, object]:
    """``det`` as one alerting rule (Prometheus and Loki share the shape)."""
    subject_label = "topic" if det.cls == "progress_stopped" else "service"
    rule: dict[str, object] = {
        "alert": "".join(part.title() for part in det.cls.split("_")),
        "expr": det.expr,
        "labels": {
            "severity": det.severity,
            "detector": det.id,
            subject_label: det.subject,
            **({"service": det.service} if det.service else {}),
        },
        "annotations": {
            "summary": det.summary,
            "description": f"SupportAgent detector {det.id} ({det.cls}); measured: "
            + ", ".join(f"{k}={v}" for k, v in sorted(det.measured.items())),
        },
    }
    if det.for_s:
        rule["for"] = _minutes(det.for_s)
    return rule


def rules_document(detectors: Sequence[SignalDetector], engine: Engine) -> dict[str, object]:
    """The enabled detectors of one engine as a Prometheus or Loki rule document."""
    rules = [alert_rule(d) for d in detectors if d.enabled and d.engine == engine]
    return {"groups": [{"name": "supportagent-detectors", "rules": rules}]}


def review_document(
    detectors: Sequence[SignalDetector], skipped: Sequence[NotProposed], system: str
) -> dict[str, object]:
    """``detectors.proposed.yaml``: every draft, disabled, for a person to review."""
    return {
        "system": system,
        "detectors": [
            {
                "id": d.id,
                "enabled": d.enabled,
                "class": d.cls,
                "subject": d.subject,
                "engine": d.engine,
                "expr": d.expr,
                "for_s": d.for_s,
                "severity": d.severity,
                "summary": d.summary,
                "measured": dict(d.measured),
                **({"service": d.service} if d.service else {}),
            }
            for d in detectors
        ],
        "not_proposed": [
            {"class": n.cls, "subject": n.subject, "reason": n.reason} for n in skipped
        ],
    }


def enabled_ids(review: object) -> frozenset[str]:
    """The ids a person enabled in a review file (anything malformed enables nothing)."""
    if not isinstance(review, Mapping):
        return frozenset()
    items = review.get("detectors")
    return frozenset(
        str(item["id"])
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, Mapping) and item.get("enabled") is True and "id" in item
    )


def with_enabled(detectors: Sequence[SignalDetector], ids: frozenset[str]) -> list[SignalDetector]:
    return [replace(d, enabled=d.id in ids) for d in detectors]


__all__ = [
    "MAX_NORMAL_QUIET_S",
    "MIN_HISTORY_STEPS",
    "MIN_IDLE_S",
    "MIN_SILENCE_S",
    "OUTLIER_SHARE",
    "STEP_S",
    "Engine",
    "NotProposed",
    "SignalClass",
    "SignalDetector",
    "Signals",
    "alert_rule",
    "detector_id",
    "draft_detectors",
    "enabled_ids",
    "longest_quiet_s",
    "quiet_runs",
    "review_document",
    "rules_document",
    "with_enabled",
]
