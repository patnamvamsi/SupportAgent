"""Measured detectors, reviewed in the instance (ADR-0023 §2 and §4, WP-223).

The ``detectors`` onboarding step measures the system's signals and stores what
it proposes here; a person enables the ones they want on
``/ui/onboarding/detectors``. Re-measuring (the history grows, a component is
added) replaces the proposals and keeps every decision: an enabled detector
stays enabled at its newly measured threshold, and one no longer proposed keeps
its last measurement while it is enabled.

How an enabled detector fires is its ``mode``: ``internal`` (the instance
evaluates it, WP-224) or ``exported`` (rendered into rule files for the team's
own Alertmanager). Never both, so nothing fires twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from supportagent.app.readiness_store import TenantDocument
from supportagent.domain.errors import DomainValidationError
from supportagent.domain.signal_detectors import NotProposed, SignalDetector, review_document
from supportagent.ports.clock import Clock

Mode = Literal["internal", "exported"]
MODES: tuple[Mode, ...] = ("internal", "exported")


class DetectorReviewStore(TenantDocument):
    table, column = "detector_review", "review_json"


class DetectorReview:
    def __init__(self, store: DetectorReviewStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def state(self) -> dict[str, Any]:
        return self._store.load() or {}

    def save_measurement(
        self,
        detectors: Sequence[SignalDetector],
        skipped: Sequence[NotProposed],
        *,
        system: str,
        days: int,
    ) -> None:
        """A new measurement; decisions are kept."""
        state = self.state()
        enabled: Mapping[str, Any] = state.get("enabled", {})
        fresh = review_document(detectors, skipped, system)
        entries = list(fresh["detectors"])  # type: ignore[call-overload]
        measured = {e["id"] for e in entries}
        entries += [
            e | {"stale": True}
            for e in state.get("detectors", [])
            if e["id"] in enabled and e["id"] not in measured
        ]
        self._store.save(
            state
            | {
                "measured_ts": self._now(),
                "days": days,
                "system": system,
                "detectors": [e | {"enabled": e["id"] in enabled} for e in entries],
                "not_proposed": fresh["not_proposed"],
            }
        )

    def decide(self, ids: Iterable[str], *, actor: str, mode: Mode = "internal") -> int:
        """Exactly ``ids`` are enabled now, in ``mode`` for newly enabled ones; returns changes."""
        if mode not in MODES:
            raise DomainValidationError(f"unknown mode {mode!r}")  # noqa: TRY003
        state = self.state()
        known = {e["id"] for e in state.get("detectors", [])}
        wanted = set(ids)
        unknown = wanted - known
        if unknown:
            raise DomainValidationError(  # noqa: TRY003
                f"no such detector: {', '.join(sorted(unknown))}"
            )
        before: dict[str, Any] = dict(state.get("enabled", {}))
        now = self._now()
        after = {i: before.get(i) or {"by": actor, "ts": now, "mode": mode} for i in sorted(wanted)}
        changed = len(set(before) ^ set(after))
        entries = [e | {"enabled": e["id"] in after} for e in state.get("detectors", [])]
        self._store.save(
            state
            | {
                "enabled": after,
                "detectors": entries,
                "reviewed_ts": now,
                "reviewed_by": actor,
                "seen": sorted(known),
            }
        )
        return changed

    def unseen(self) -> list[str]:
        """Proposed detectors no one has looked at since they were measured."""
        state = self.state()
        seen = set(state.get("seen", []))
        return sorted(
            e["id"]
            for e in state.get("detectors", [])
            if e["id"] not in seen and not e.get("stale")
        )

    def enabled(self, mode: Mode | None = None) -> list[SignalDetector]:
        """The enabled detectors (of one mode), as last measured."""
        state = self.state()
        chosen: Mapping[str, Any] = state.get("enabled", {})
        return [
            _detector(e)
            for e in state.get("detectors", [])
            if e["id"] in chosen and (mode is None or chosen[e["id"]].get("mode") == mode)
        ]

    def _now(self) -> int:
        return int(self._clock.now().timestamp())


def _detector(entry: Mapping[str, Any]) -> SignalDetector:
    return SignalDetector(
        id=str(entry["id"]),
        cls=entry["class"],
        subject=str(entry["subject"]),
        engine=entry["engine"],
        expr=str(entry["expr"]),
        for_s=int(entry.get("for_s", 0)),
        severity=str(entry.get("severity", "critical")),
        summary=str(entry.get("summary", "")),
        measured=dict(entry.get("measured", {})),
        enabled=True,
    )


__all__ = ["MODES", "DetectorReview", "DetectorReviewStore", "Mode"]
