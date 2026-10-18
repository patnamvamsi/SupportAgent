"""Core ``Table`` definitions — the single DDL source for 0001 (03 §2, §4).

Migration 0001 renders these against the target dialect; the invariant suite
(``test_schema_enums_match_domain``) introspects the resulting CHECK
constraints to keep literal sets in lockstep with the domain enums (03 §1).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from supportagent.domain.events import EventKind
from supportagent.domain.lifecycle import CaseStatus
from supportagent.domain.severity import Severity

metadata = sa.MetaData()

# 03 §2.1: role literals. A domain Role enum lands in v0.1 (WP-103); until
# then the literal set is pinned here and by the invariant test.
ROLES: tuple[str, ...] = ("viewer", "analyst", "approver", "admin")
# 03 §2.2: origin literals; cf. ``domain.model.alert.AlertEvent.origin``.
ORIGINS: tuple[str, ...] = ("alert", "ticket")


def _check(column: str, values: Sequence[str]) -> sa.CheckConstraint:
    """``CHECK (<column> IN (...))`` with literals from the domain enums (03 §1)."""
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(value) for value in values)})")


# --- 03 §2.1: identity -------------------------------------------------------

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("is_active", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("created_ts", sa.Integer, nullable=False),
    sa.Column("last_login_ts", sa.Integer),
)

role_assignments = sa.Table(
    "role_assignments",
    metadata,
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
    sa.Column("role", sa.Text, nullable=False, primary_key=True),
    sa.Column("scope", sa.Text, nullable=False, server_default=sa.text("'*'")),
    sa.Column("granted_by", sa.Text, sa.ForeignKey("users.id")),
    sa.Column("granted_ts", sa.Integer, nullable=False),
    _check("role", ROLES),
)

# --- 03 §2.2: cases ----------------------------------------------------------

case_groups = sa.Table(
    "case_groups",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("opened_ts", sa.Integer, nullable=False),
    sa.Column("closed_ts", sa.Integer),
    sa.Column("rule", sa.Text, nullable=False),
    sa.Column("member_count", sa.Integer, nullable=False, server_default=sa.text("0")),
)

cases = sa.Table(
    "cases",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("external_table", sa.Text),
    sa.Column("external_id", sa.Text),
    sa.Column("ticket_number", sa.Text),
    sa.Column("service", sa.Text),
    sa.Column("classification", sa.Text),
    sa.Column("severity", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("decision_json", sa.Text),
    sa.Column("group_id", sa.Text, sa.ForeignKey("case_groups.id")),
    sa.Column("duplicate_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("opened_ts", sa.Integer, nullable=False),
    sa.Column("last_alert_ts", sa.Integer, nullable=False),
    sa.Column("resolved_ts", sa.Integer),
    sa.Column("trace_id", sa.Text, nullable=False),
    _check("origin", ORIGINS),
    _check("severity", tuple(sev.value for sev in Severity)),
    _check("status", tuple(st.value for st in CaseStatus)),
    # 03 §2.2: one unresolved case per fingerprint per tenant (partial unique).
    sa.Index(
        "ux_cases_unresolved",
        "tenant_id",
        "fingerprint",
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
        sqlite_where=sa.text("status <> 'resolved'"),
    ),
    sa.Index("ix_cases_status", "tenant_id", "status", "opened_ts"),
    sa.Index("ix_cases_external", "tenant_id", "external_table", "external_id"),
)

# --- 03 §2.2: alerts (minimal ingest row; the event store lands in 0002) ----

alerts = sa.Table(
    "alerts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
    sa.Column("adapter", sa.Text, nullable=False),
    sa.Column("external_id", sa.Text, nullable=False),
    sa.Column("payload_ref", sa.Text, nullable=False),
    sa.Column("received_ts", sa.Integer, nullable=False),
    sa.Index("ix_alerts_case", "tenant_id", "case_id"),
)

# --- 03 §2.5: leases (global lock table — deliberately no tenant_id) --------

leases = sa.Table(
    "leases",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("until_ts", sa.Integer, nullable=False),
)

# --- 03 §3: blobs + wrapped tenant data keys ---------------------------------

blobs = sa.Table(
    "blobs",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("data", sa.LargeBinary, nullable=False),
)

tenant_keys = sa.Table(
    "tenant_keys",
    metadata,
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("wrapped_key", sa.LargeBinary, nullable=False),
    sa.Column("created_ts", sa.Integer, nullable=False),
)

# --- 03 §2.4: the append-only event log --------------------------------------

EVENT_KINDS: tuple[str, ...] = tuple(k.value for k in EventKind)  # 03 §1 lockstep
EVENT_STATUSES: tuple[str, ...] = ("ok", "error", "skipped")  # 01 §4 Event.status Literal

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("trace_id", sa.Text, nullable=False),
    sa.Column("span_id", sa.Text, nullable=False),
    sa.Column("parent_span_id", sa.Text),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("model", sa.Text),
    sa.Column("tokens_in", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("tokens_out", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("latency_ms", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.Text, nullable=False),
    # AES-256-GCM envelope of the canonical JSON payload (03 §2.4, §2.6).
    sa.Column("payload_enc", sa.LargeBinary, nullable=False),
    # Epoch milliseconds (03 §1); 64-bit so Postgres INT4 cannot overflow.
    sa.Column("ts_ms", sa.BigInteger, nullable=False),
    _check("kind", EVENT_KINDS),
    _check("status", EVENT_STATUSES),
    sa.Index("ix_events_trace", "tenant_id", "trace_id", "ts_ms"),
    sa.Index("ix_events_kind", "tenant_id", "kind", "ts_ms"),
)

# --- 03 §4: the 10-wp human gate (proposals/approvals/grants/comms/outbox) ----
#
# The enforcement of INV-01 itself lives in migration 0003's triggers
# (``trg_outbox_inv01`` etc.); here we declare the tables so the DDL is a
# single source and the schema/introspection tests see the full head set.

from supportagent.domain.model.comms import CommsPlanStatus  # noqa: E402
from supportagent.domain.model.grant import GrantStatus  # noqa: E402
from supportagent.domain.proposal_state import ProposalStatus  # noqa: E402  (import after module body, alembic-style grouping)

PROPOSAL_STATUSES: tuple[str, ...] = tuple(s.value for s in ProposalStatus)  # 03 §1 lockstep
APPROVAL_STATUSES: tuple[str, ...] = ("approved", "rejected")
POLICY_GRANT_STATUSES: tuple[str, ...] = tuple(s.value for s in GrantStatus)  # ARCH §6.2
COMMS_PLAN_STATUSES: tuple[str, ...] = tuple(s.value for s in CommsPlanStatus)  # ARCH §6.3
OUTBOX_STATUSES: tuple[str, ...] = ("pending", "claimed", "done", "failed")  # ARCH §6.2
AUDIENCES: tuple[str, ...] = ("customer", "internal")  # ARCH §6.3
COMMS_KINDS: tuple[str, ...] = ("work_note", "customer_comment", "send_email_customer")
COMMS_STATUSES: tuple[str, ...] = ("queued", "sent", "failed")  # ARCH §6.3 comms history

# ARCH §6.2: exactly one authorization path backs an outbox row. CASE keeps
# the sum portable (Postgres has no bool + bool).
EXACTLY_ONE_AUTH_PATH = (
    "(CASE WHEN approval_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN policy_grant_id IS NOT NULL THEN 1 ELSE 0 END"
    " + CASE WHEN comms_plan_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)

proposals = sa.Table(
    "proposals",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
    sa.Column("action_kind", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("trace_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'proposed'")),
    sa.Column("created_by", sa.Text, nullable=False),
    # Epoch seconds (03 §1); 64-bit so Postgres INT4 cannot overflow.
    sa.Column("created_at_ts", sa.BigInteger, nullable=False),
    sa.Column("expires_ts", sa.BigInteger),
    _check("status", PROPOSAL_STATUSES),
    sa.Index("ix_proposals_case", "tenant_id", "case_id"),
    sa.Index("ix_proposals_pending", "tenant_id", "status", "expires_ts"),  # 03 §2.3
)

approvals = sa.Table(
    "approvals",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("proposal_id", sa.Text, sa.ForeignKey("proposals.id"), nullable=False),
    sa.Column("approver_id", sa.Text, sa.ForeignKey("users.id"), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("approved_at_ts", sa.BigInteger, nullable=False),
    sa.Column("trace_id", sa.Text),
    _check("status", APPROVAL_STATUSES),
    sa.Index("ix_approvals_proposal", "proposal_id"),
)

policy_grants = sa.Table(
    "policy_grants",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("action_kind", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'requested'")),
    # ARCH §6.2: users.id | 'config:<sha>' (from `grants request`), so no FK.
    sa.Column("requested_by", sa.Text, nullable=False),
    sa.Column("requested_at_ts", sa.BigInteger, nullable=False),
    sa.Column("granted_by", sa.Text, sa.ForeignKey("users.id")),
    sa.Column("granted_at_ts", sa.BigInteger),
    sa.Column("valid_to_ts", sa.BigInteger),
    _check("status", POLICY_GRANT_STATUSES),
    # INV-01: self-approval — a grant cannot be approved by its own requester
    # (enforced as a trigger in 0003 as well; the CHECK is the portable floor).
    sa.CheckConstraint(
        "status <> 'approved' "
        "OR (granted_by IS NOT NULL AND granted_at_ts IS NOT NULL "
        "AND valid_to_ts IS NOT NULL AND granted_by <> requested_by)",
        name="ck_policy_grants_no_self_approval",
    ),
    sa.Index("ix_policy_grants_lookup", "tenant_id", "action_kind", "scope"),
)

comms_plans = sa.Table(
    "comms_plans",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
    sa.Column("audience", sa.Text, nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("template_id", sa.Text, nullable=False),
    sa.Column("allowed_fields_json", sa.Text, nullable=False),
    sa.Column("cadence_minutes", sa.Integer),
    sa.Column("plan_hash", sa.Text, nullable=False),
    # ARCH §6.3: the plan is a proposal (kind 'comms_plan').
    sa.Column("proposal_id", sa.Text, sa.ForeignKey("proposals.id"), nullable=False),
    sa.Column("approval_id", sa.Text, sa.ForeignKey("approvals.id")),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending_approval'")),
    _check("status", COMMS_PLAN_STATUSES),
    _check("audience", AUDIENCES),
    sa.Index("ix_comms_plans_proposal", "proposal_id"),
)

outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
    sa.Column("action_kind", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False),
    sa.Column("payload_hash", sa.Text, nullable=False),
    # INV-04: one row per idempotency key (ARCH §6.2).
    sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
    # Approval path is single-use (ARCH §6.2: UNIQUE).
    sa.Column("approval_id", sa.Text, sa.ForeignKey("approvals.id"), unique=True),
    sa.Column("policy_grant_id", sa.Text, sa.ForeignKey("policy_grants.id")),
    sa.Column("comms_plan_id", sa.Text, sa.ForeignKey("comms_plans.id")),
    sa.Column("trace_id", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
    # 03 §2.3: executor bookkeeping; trg_outbox_immutable leaves these mutable.
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("next_attempt_ts", sa.BigInteger),
    sa.Column("last_error", sa.Text),
    sa.Column("external_ref", sa.Text),
    sa.Column("claimed_at_ts", sa.BigInteger),
    sa.Column("done_at_ts", sa.BigInteger),
    _check("status", OUTBOX_STATUSES),
    sa.CheckConstraint(EXACTLY_ONE_AUTH_PATH, name="ck_outbox_one_auth_path"),
    sa.Index("ix_outbox_due", "tenant_id", "status", "next_attempt_ts"),  # 03 §2.3
)

# ARCH §6.3: the comms history / rendering record. Every send IS an outbox row.
comms = sa.Table(
    "comms",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("case_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("plan_id", sa.Text, sa.ForeignKey("comms_plans.id")),
    sa.Column("outbox_id", sa.Text, sa.ForeignKey("outbox.id"), nullable=False, unique=True),
    sa.Column("body_json", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
    sa.Column("sent_at_ts", sa.BigInteger),
    _check("kind", COMMS_KINDS),
    _check("status", COMMS_STATUSES),
)
