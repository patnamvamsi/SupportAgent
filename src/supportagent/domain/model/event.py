"""Event: one row of the append-only event log (01 §4, ARCH §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import field_validator

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

    @field_validator("payload")
    @classmethod
    def _payload_has_version(cls, value: dict[str, object]) -> dict[str, object]:
        # 01 §7.1: floats are forbidden in hashed payloads.
        _reject_floats(value)
        if "v" not in value or not isinstance(value["v"], int) or isinstance(value["v"], bool):
            raise ValueError(  # noqa: TRY003 — message is the pinned validation API (01 §4)
                "payload must contain 'v': <int> payload schema version"
            )
        return value
