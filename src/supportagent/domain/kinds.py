"""Action kinds and their tier/audience/paths metadata (ARCH §12.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from supportagent.domain.tiers import Tier


class ActionKind(StrEnum):
    WORK_NOTE = "work_note"
    ENRICH_PAGE = "enrich_page"
    WATCH_LIST_ADD_INTERNAL = "watch_list_add_internal"
    WATCH_LIST_ADD_CUSTOMER = "watch_list_add_customer"
    CUSTOMER_COMMENT = "customer_comment"
    SEND_EMAIL_CUSTOMER = "send_email_customer"
    CREATE_TICKET_CUSTOMER = "create_ticket_customer"
    TICKET_UPDATE = "ticket_update"
    JOB_RERUN = "job_rerun"
    FULFILL_REQUEST = "fulfill_request"
    COMMS_PLAN = "comms_plan"
    CREATE_PAGE = "create_page"


@dataclass(frozen=True, slots=True)
class KindMeta:
    """Tier, audience and authorization paths for one kind (ARCH §12.2)."""

    tier: Tier | None
    audience: str
    paths: str


# The 12 kinds of ARCH §12.2, verbatim (cell text after ** and ` stripping).
KIND_META: Mapping[ActionKind, KindMeta] = {
    ActionKind.WORK_NOTE: KindMeta(
        Tier.T1, "internal", "grant (customer-approved at setup) or approval"
    ),
    ActionKind.ENRICH_PAGE: KindMeta(Tier.T1, "internal", "grant or approval"),
    ActionKind.WATCH_LIST_ADD_INTERNAL: KindMeta(Tier.T1, "internal", "grant or approval"),
    ActionKind.WATCH_LIST_ADD_CUSTOMER: KindMeta(Tier.T2, "customer", "approval only"),
    ActionKind.CUSTOMER_COMMENT: KindMeta(Tier.T2, "customer", "approval or approved comms plan"),
    ActionKind.SEND_EMAIL_CUSTOMER: KindMeta(
        Tier.T2, "customer", "approval or approved comms plan"
    ),
    ActionKind.CREATE_TICKET_CUSTOMER: KindMeta(
        Tier.T2, "customer", "approval only (two-person, v0.2)"
    ),
    ActionKind.TICKET_UPDATE: KindMeta(Tier.T2, "internal", "approval or literal-scope grant"),
    ActionKind.JOB_RERUN: KindMeta(
        Tier.T2, "internal", "approval or literal-scope grant (two-person, v0.2)"
    ),
    ActionKind.FULFILL_REQUEST: KindMeta(
        Tier.T2, "depends on the catalog item", "approval or literal-scope grant"
    ),
    ActionKind.COMMS_PLAN: KindMeta(Tier.T2, "(meta)", "approval only"),
    ActionKind.CREATE_PAGE: KindMeta(None, "—", "never executable in v0.x"),
}

# 01 §7.7: the kinds whose effect reaches the customer.
CUSTOMER_FACING_KINDS: frozenset[ActionKind] = frozenset(
    {
        ActionKind.WATCH_LIST_ADD_CUSTOMER,
        ActionKind.CUSTOMER_COMMENT,
        ActionKind.SEND_EMAIL_CUSTOMER,
        ActionKind.CREATE_TICKET_CUSTOMER,
    }
)

# 01 §7.7: never executable in v0.x — recommend only.
NEVER_EXECUTABLE_KINDS: frozenset[ActionKind] = frozenset({ActionKind.CREATE_PAGE})
