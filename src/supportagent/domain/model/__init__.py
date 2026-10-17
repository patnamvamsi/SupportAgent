"""Domain models (01 §4): frozen, extra-forbid, strict, aware-UTC."""

from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.approval import Approval, ApprovalStatus
from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.comms import CommsPlan, CommsPlanStatus
from supportagent.domain.model.event import Event
from supportagent.domain.model.grant import Grant, GrantStatus
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.model.triage import TriageDecision, TriageResult
from supportagent.domain.model.user import User

__all__ = [
    "ActionSpec",
    "AlertEvent",
    "Approval",
    "ApprovalStatus",
    "CommsPlan",
    "CommsPlanStatus",
    "DomainModel",
    "Event",
    "Grant",
    "GrantStatus",
    "Proposal",
    "TargetRef",
    "TriageDecision",
    "TriageResult",
    "User",
]
