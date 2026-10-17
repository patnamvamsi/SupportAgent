"""ActionSpec and TargetRef: a proposed action (01 §4)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from supportagent.domain.hashing import _reject_floats
from supportagent.domain.kinds import ActionKind
from supportagent.domain.model.base import DomainModel


class TargetRef(DomainModel):
    """Where the action acts (01 §4)."""

    service: str
    system: Literal["servicenow", "airflow", "gha", "pager", "email"]
    ref: str


class ActionSpec(DomainModel):
    """A proposed action: kind, target, scope, hashable payload (01 §4, §7.1)."""

    kind: ActionKind
    target: TargetRef
    scope: str = Field(min_length=1)
    payload: dict[str, object]  # JsonValue domain; floats rejected by the validator

    @field_validator("payload")
    @classmethod
    def _no_floats_in_payload(cls, value: dict[str, object]) -> dict[str, object]:
        # 01 §7.1: floats are forbidden in hashed payloads.
        _reject_floats(value)
        return value
