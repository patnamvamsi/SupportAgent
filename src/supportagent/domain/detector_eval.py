"""Evaluating detectors in the instance (ADR-0023 §4, WP-224).

A detector's query returns a series while its condition holds. Evaluated on an
interval, each detector is:

- **pending** from the first evaluation whose query returns a series;
- **firing** once the condition has held for its ``for_s`` (at once when 0),
  which sends one firing alert;
- **resolved** at the first evaluation whose query returns nothing, which
  sends one resolved alert.

An evaluation that fails (Prometheus or Loki unreachable, a bad query) changes
nothing: a monitoring outage is neither a failure nor a recovery. A detector
that is turned off while firing is resolved, so no case waits on an alert that
can't end.

The alert is Alertmanager's webhook shape, so it enters the same intake as a
webhook delivery (``domain.alertmanager``) and becomes an ``AlertEvent`` the
same way. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from supportagent.domain.signal_detectors import SignalDetector, alert_rule

Transition = Literal["fire", "resolve"]


@dataclass(frozen=True, slots=True)
class Watch:
    """One detector's evaluation state: when its condition began holding, and when it fired."""

    since: int | None = None
    fired: int | None = None


def observe(watch: Watch, holds: bool, now: int, for_s: int) -> tuple[Watch, Transition | None]:
    """The next state after one evaluation, and the alert it sends (if any)."""
    if not holds:
        return Watch(), ("resolve" if watch.fired is not None else None)
    since = watch.since if watch.since is not None else now
    if watch.fired is None and now - since >= for_s:
        return Watch(since, now), "fire"
    return Watch(since, watch.fired), None


def alert(
    det: SignalDetector, transition: Transition, *, fired: int, now: int
) -> dict[str, object]:
    """One Alertmanager-shaped alert for ``det`` firing or resolving."""
    rule = alert_rule(det)
    labels = dict(rule["labels"])  # type: ignore[call-overload]
    labels["alertname"] = str(rule["alert"])
    out: dict[str, object] = {
        "status": "firing" if transition == "fire" else "resolved",
        "labels": labels,
        "annotations": dict(rule["annotations"]),  # type: ignore[call-overload]
        "startsAt": _iso(fired),
        "fingerprint": f"detector-{det.id}",
        "generatorURL": "/ui/onboarding/detectors",
    }
    if transition == "resolve":
        out["endsAt"] = _iso(now)
    return out


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["Transition", "Watch", "alert", "observe"]
