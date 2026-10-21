"""Id prefixes and branded ids (03 §1: 13 prefixes, <prefix>_<ULID>)."""

from __future__ import annotations

from enum import StrEnum
from typing import NewType


class IdPrefix(StrEnum):
    CASE = "case"
    ALERT = "alrt"
    PROPERTY = "prop"
    APPROVAL = "appr"
    GRANT = "grnt"
    OUTBOX = "obx"
    PLAN = "plan"
    COMMS = "cmms"
    EVENT = "evt"
    GROUP = "grp"
    SHADOW = "shdw"
    FEEDBACK = "fb"
    VERSION = "ver"
    RCA = "rca"


# Branded ids (05 §6): distinct types at module boundaries. Users are
# identified by their OIDC sub (01 §3), so there is no user prefix.
CaseId = NewType("CaseId", str)
AlertId = NewType("AlertId", str)
ProposalId = NewType("ProposalId", str)
ApprovalId = NewType("ApprovalId", str)
GrantId = NewType("GrantId", str)
OutboxId = NewType("OutboxId", str)
CommsPlanId = NewType("CommsPlanId", str)
CommsId = NewType("CommsId", str)
EventId = NewType("EventId", str)
CaseGroupId = NewType("CaseGroupId", str)
ShadowId = NewType("ShadowId", str)
FeedbackId = NewType("FeedbackId", str)
VersionId = NewType("VersionId", str)
RcaId = NewType("RcaId", str)
