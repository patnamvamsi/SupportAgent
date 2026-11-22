"""Onboarding driven from the instance (ADR-0023 §2, WP-223).

The system manifest is stored per tenant and edited on ``/ui/onboarding/system``;
"Run now" records a request; the ``onboard.autopilot`` worker job claims a run
when one is requested or the schedule is due, runs every step, and records
how it ended. Only one run is in flight per tenant: a claim older than
:data:`STALE_RUN_S` (a worker that died mid-run) can be claimed again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from supportagent.app.readiness_store import TenantDocument
from supportagent.domain.errors import DomainValidationError
from supportagent.ports.clock import Clock

#: A run still marked in flight after this long is presumed dead.
STALE_RUN_S = 6 * 3600
_MAX_MANIFEST_BYTES = 64 * 1024


class OnboardingControlStore(TenantDocument):
    """The manifest a person edits, and the autopilot's run requests and state."""

    table, column = "onboarding_control", "control_json"


@dataclass(frozen=True, slots=True)
class Claim:
    """A run the job may start: why (``requested`` or ``scheduled``) and by whom."""

    reason: str
    actor: str


class OnboardingControl:
    def __init__(
        self,
        store: OnboardingControlStore,
        clock: Clock,
        *,
        validate: Callable[[str], str] | None = None,
    ) -> None:
        """``validate`` checks a manifest's text and returns its system id (or raises)."""
        self._store = store
        self._clock = clock
        self._validate = validate

    def state(self) -> dict[str, Any]:
        return self._store.load() or {}

    def manifest(self) -> str | None:
        text = self.state().get("manifest")
        return str(text) if text else None

    def save_manifest(self, text: str, *, actor: str) -> str:
        """Store a person's manifest after it validates; returns its system id."""
        if len(text.encode()) > _MAX_MANIFEST_BYTES:
            raise DomainValidationError("the manifest is larger than 64 KiB")  # noqa: TRY003
        system = self._validate(text) if self._validate is not None else ""
        now = self._now()
        self._save(manifest=text, manifest_by=actor, manifest_ts=now, system=system)
        return system

    def request_run(self, *, actor: str) -> None:
        if not self.manifest():
            raise DomainValidationError("describe the system first (its manifest)")  # noqa: TRY003
        self._save(requested_ts=self._now(), requested_by=actor)

    def claim(self, *, interval_s: float) -> Claim | None:
        """A run to start now, or ``None``: none requested or due, or one in flight."""
        state = self.state()
        if not state.get("manifest"):
            return None
        now = self._now()
        started = int(state.get("started_ts") or 0)
        finished = int(state.get("finished_ts") or 0)
        if started > finished and now - started < STALE_RUN_S:
            return None
        requested = int(state.get("requested_ts") or 0)
        if requested and requested >= finished:
            claim = Claim("requested", str(state.get("requested_by") or ""))
        elif interval_s > 0 and now - finished >= interval_s:
            claim = Claim("scheduled", "worker")
        else:
            return None
        self._save(started_ts=now, started_reason=claim.reason, requested_ts=0)
        return claim

    def finish(self, *, ok: bool, detail: str) -> None:
        self._save(finished_ts=self._now(), last_ok=ok, last_detail=detail[:500])

    def _now(self) -> int:
        return int(self._clock.now().timestamp())

    def _save(self, **changes: object) -> None:
        self._store.save({**self.state(), **changes})


__all__ = ["STALE_RUN_S", "Claim", "OnboardingControl", "OnboardingControlStore"]
