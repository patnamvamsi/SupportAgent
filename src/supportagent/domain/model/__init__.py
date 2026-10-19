"""Domain models (01 §4): frozen, extra-forbid, strict, aware-UTC."""

from supportagent.domain.model.action import ActionSpec, TargetRef
from supportagent.domain.model.alert import AlertEvent
from supportagent.domain.model.approval import Approval, ApprovalStatus
from supportagent.domain.model.base import DomainModel
from supportagent.domain.model.calibration import (
    CalibrationCountRow,
    CalibrationLabelRow,
    CalibrationTable,
)
from supportagent.domain.model.case import Case
from supportagent.domain.model.catalog import Catalog, ServiceEntry
from supportagent.domain.model.comms import CommsPlan, CommsPlanStatus
from supportagent.domain.model.event import Event
from supportagent.domain.model.external import ExternalCaseRef, ExternalRef
from supportagent.domain.model.grant import Grant, GrantStatus
from supportagent.domain.model.preclass import PreclassRule, RuleSet
from supportagent.domain.model.proposal import Proposal
from supportagent.domain.model.routing import Route, RoutingRule, RoutingTable
from supportagent.domain.model.taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    Hypothesis,
    HypothesisTaxonomy,
)
from supportagent.domain.model.ticket import ChangeRecord, TicketRecord
from supportagent.domain.model.triage import TriageDecision, TriageResult
from supportagent.domain.model.user import User

__all__ = [
    "ActionSpec",
    "AlertEvent",
    "Approval",
    "ApprovalStatus",
    "CalibrationCountRow",
    "CalibrationLabelRow",
    "CalibrationTable",
    "Case",
    "Catalog",
    "ChangeRecord",
    "ClassificationLabel",
    "ClassificationTaxonomy",
    "CommsPlan",
    "CommsPlanStatus",
    "DomainModel",
    "Event",
    "ExternalCaseRef",
    "ExternalRef",
    "Grant",
    "GrantStatus",
    "Hypothesis",
    "HypothesisTaxonomy",
    "PreclassRule",
    "Proposal",
    "Route",
    "RoutingRule",
    "RoutingTable",
    "RuleSet",
    "ServiceEntry",
    "TargetRef",
    "TicketRecord",
    "TriageDecision",
    "TriageResult",
    "User",
]
