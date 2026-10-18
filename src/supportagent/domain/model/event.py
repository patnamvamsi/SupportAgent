"""Event: one row of the append-only event log (01 §4, ARCH §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from supportagent.domain.errors import DomainValidationError
from supportagent.domain.events import EventKind
from supportagent.domain.hashing import _reject_floats
from supportagent.domain.model.base import DomainModel


class Event(DomainModel):
    """An immutable audit event (01 §4)."""

    id: str
    tenant_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: EventKind
    actor: str
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    status: Literal["ok", "error", "skipped"] = "ok"
    payload: dict[str, object]  # JsonValue domain; must contain "v": <int>
    ts: datetime

    def _assert_domain_rules(self) -> None:
        # 01 §7.1: floats are forbidden in hashed payloads.
        _reject_floats(self.payload)
        version = self.payload.get("v")
        if not isinstance(version, int) or isinstance(version, bool):
            # 10-wp WP-004: a missing 'v' is a DomainValidationError.
            raise DomainValidationError(  # noqa: TRY003 — pinned validation API
                "payload must contain 'v': <int> payload schema version"
            )
