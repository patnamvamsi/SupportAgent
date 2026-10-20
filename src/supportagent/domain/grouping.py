"""Storm grouping (ARCH §4.3, 01 §7.8; 11-wp WP-111): link cases, never merge them.

``assign_group`` is pure: given a new case and the tenant's open groups, it
decides whether the case **joins** a group, **opens** one, or stays
ungrouped.

1. Candidates are open groups opened within the window (10 min, prov.), oldest first.
2. Join the first candidate where a member shares the case's service, or
   where the two services are linked by ``depends_on`` (either direction),
   or where at least 5 (prov.) members share the case's upstream source.
3. Otherwise, in **storm mode** (more than 20 new cases in 5 min, prov.),
   open a new group led by this case. Outside storm mode the case stays
   ungrouped.

The **upstream source** is the adapter plus the alert type (``servicenow:<alert
type>``; for ServiceNow the alert type is ``u_alert_source``, the monitoring
tool). The adapter alone would put every case of a storm in one group.

A **storm member** is a case that joins a group while storm mode is on. It
is not triaged on its own: it inherits the lead's route as a suggestion and
goes to the human queue, so N alerts cost one LLM classification (ARCH §4.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from supportagent.domain.model.catalog import Catalog

WINDOW_S = 600  # (prov.) 10 minutes
SOURCE_THRESHOLD = 5  # (prov.)
STORM_CASES = 20  # (prov.) more than this many new cases ...
STORM_WINDOW_S = 300  # (prov.) ... in 5 minutes
IDLE_CLOSE_S = 86_400  # a group closes after 24 h without new members

GroupRule = Literal["same_service", "dependency", "shared_source", "storm"]


@dataclass(frozen=True, slots=True)
class GroupMember:
    case_id: str
    service: str | None
    source: str
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class OpenGroup:
    id: str
    opened_ts: int
    lead_case_id: str
    members: Sequence[GroupMember]
    last_member_ts: int


@dataclass(frozen=True, slots=True)
class GroupDecision:
    action: Literal["join", "open", "none"]
    group_id: str | None = None
    rule: GroupRule | None = None
    storm: bool = False

    @property
    def storm_member(self) -> bool:
        """Joined an existing group during a storm: skip its own triage."""
        return self.action == "join" and self.storm


def related(a: str | None, b: str | None, catalog: Catalog) -> bool:
    """A ``depends_on`` edge between two services, either direction."""
    if a is None or b is None or a == b:
        return False
    sa, sb = catalog.service(a), catalog.service(b)
    return bool((sa and b in sa.depends_on) or (sb and a in sb.depends_on))


def assign_group(  # noqa: PLR0913 — the 01 §7.8 inputs
    *,
    service: str | None,
    source: str,
    now_ts: int,
    open_groups: Sequence[OpenGroup],
    recent_new_cases: int,
    catalog: Catalog,
) -> GroupDecision:
    """01 §7.8 steps 1-3. ``recent_new_cases`` excludes this case."""
    storm = recent_new_cases > STORM_CASES
    candidates = sorted(
        (g for g in open_groups if g.opened_ts >= now_ts - WINDOW_S),
        key=lambda g: (g.opened_ts, g.id),
    )
    for group in candidates:
        rule = _join_rule(group, service, source, catalog)
        if rule is not None:
            return GroupDecision("join", group.id, rule, storm)
    if storm:
        return GroupDecision("open", None, "storm", storm)
    return GroupDecision("none", storm=False)


def _join_rule(
    group: OpenGroup, service: str | None, source: str, catalog: Catalog
) -> GroupRule | None:
    if service is not None and any(m.service == service for m in group.members):
        return "same_service"
    if any(related(service, m.service, catalog) for m in group.members):
        return "dependency"
    if sum(1 for m in group.members if m.source == source) >= SOURCE_THRESHOLD:
        return "shared_source"
    return None


def should_close(group: OpenGroup, now_ts: int) -> bool:
    """01 §7.8 step 4: every member resolved, or 24 h without a new member."""
    if group.members and all(m.resolved for m in group.members):
        return True
    return now_ts - group.last_member_ts >= IDLE_CLOSE_S


__all__ = [
    "IDLE_CLOSE_S",
    "SOURCE_THRESHOLD",
    "STORM_CASES",
    "STORM_WINDOW_S",
    "WINDOW_S",
    "GroupDecision",
    "GroupMember",
    "GroupRule",
    "OpenGroup",
    "assign_group",
    "related",
    "should_close",
]
