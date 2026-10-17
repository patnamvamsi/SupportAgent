"""AlertEvent: the normalized ingest record (01 §4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity


class AlertEvent(DomainModel):
    tenant_id: str
    source: Literal["servicenow", "webhook"]
    origin: Literal["alert", "ticket"]
    external_id: str
    table: str | None
    ticket_number: str | None  # required when origin == "ticket"
    received_at: datetime
    subject: str = Field(max_length=4000)
    body: str = Field(max_length=65536)
    resource: str | None
    alert_type: str | None
    error_signature: str | None
    severity_hint: Severity | None
    cmdb_ci: str | None
    raw_ref: str

    @model_validator(mode="after")
    def _ticket_requires_number(self) -> Self:
        if self.origin == "ticket" and not self.ticket_number:
            raise ValueError(  # noqa: TRY003 — message is the pinned validation API (01 §4)
                "ticket_number is required when origin is 'ticket'"
            )
        return self
