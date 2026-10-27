"""ServiceNow's connector manifest and ticket semantics (WP-M07).

What the core knows about ServiceNow comes from here, never from a vendor
name in core code: the incident fields that drive takeover, the states that
end a case, priority to severity (ADR-0009), priority escalation (BL-047)
and the display values customers see.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from supportagent.domain.connector import Capability, ConnectorManifest
from supportagent.domain.severity import Severity

#: The Table API can do everything the agent needs.
SERVICENOW_MANIFEST = ConnectorManifest(name="servicenow", capabilities=frozenset(Capability))

#: Incident states that end a case (01 §7.11 "resolved/closed"): resolved, closed, canceled.
DEFAULT_RESOLVED_STATES = frozenset({"6", "7", "8"})

#: Display values for the record_state fields customers see (ServiceNow defaults).
STATE_LABELS: Mapping[str, str] = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}
PRIORITY_LABELS: Mapping[str, str] = {
    "1": "1 - Critical",
    "2": "2 - High",
    "3": "3 - Moderate",
    "4": "4 - Low",
}
_PLANNING = 5  # ServiceNow's Planning priority


def severity_from_priority(priority: str | None) -> Severity | None:
    """Priority ``"1"``..``"4"`` → ``P1``..``P4`` (02 §6); ``"5"`` (Planning) → ``P4``.

    Anything else → ``None``.

    ADR-0009: this is the severity a case routes on, so the lowest
    ServiceNow priority still yields one rather than falling back to the LLM.
    """
    if priority is None:
        return None
    try:
        level = int(priority)
    except ValueError:
        return None
    if 1 <= level <= 4:  # noqa: PLR2004
        return Severity(f"P{level}")
    if level == _PLANNING:
        return Severity.P4
    return None


@dataclass(frozen=True, slots=True)
class ServiceNowSemantics:
    """``TicketSemantics`` for a ServiceNow incident table."""

    resolved_states: frozenset[str] = DEFAULT_RESOLVED_STATES
    state_field: str = "state"
    assignee_field: str = "assigned_to"
    priority_field: str = "priority"
    labels: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: {"state": STATE_LABELS, "priority": PRIORITY_LABELS}
    )

    @classmethod
    def with_resolved_states(cls, states: Collection[str] | None) -> ServiceNowSemantics:
        """BL-032: an instance with custom states configures its own."""
        return cls(resolved_states=frozenset(states)) if states else cls()

    def severity_for(self, priority: str | None) -> Severity | None:
        return severity_from_priority(priority)

    def priority_raised(self, old: str | None, new: str | None) -> bool:
        """A higher priority is a lower number; a missing or non-numeric value never is."""
        try:
            return int(new or "") < int(old or "")
        except ValueError:
            return False

    def display(self, field: str, raw: str) -> str:
        return self.labels.get(field, {}).get(raw, raw)


__all__ = [
    "DEFAULT_RESOLVED_STATES",
    "PRIORITY_LABELS",
    "SERVICENOW_MANIFEST",
    "STATE_LABELS",
    "ServiceNowSemantics",
    "severity_from_priority",
]
