"""Shadow-mode values (ARCH §8.4, 03 §2.6, 10-wp WP-014)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.base import DomainModel
from supportagent.domain.severity import Severity


class ShadowRecord(DomainModel):
    """What the agent would have done for one case, and when (03 §2.6).

    ``proposed`` holds the decision and the ActionSpecs that would have been
    submitted; ``first_proposal_at`` is None on a safe default. The human
    fields and ``agreement`` stay None until the human label is known.
    """

    id: str
    tenant_id: str
    case_id: str
    alert_received_at: datetime
    first_proposal_at: datetime | None
    proposed: Mapping[str, object]
    human_classification: str | None = None
    human_severity: Severity | None = None
    human_first_action_at: datetime | None = None
    agreement: bool | None = None


class HistoricalIncident(DomainModel):
    """One partner incident to replay through shadow (``shadow import``)."""

    evt: AlertEvent
    opened_at: datetime
    human_classification: str | None
    human_severity: Severity | None
    human_first_action_at: datetime | None = None
