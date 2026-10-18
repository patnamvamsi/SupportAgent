"""0001_base: the v0.0 base schema (03 §2, §4).

Eight tables — ``users``, ``role_assignments``, ``case_groups``, ``cases``,
``alerts``, ``leases``, ``blobs``, ``tenant_keys`` — plus the four indexes
named in 03 §2.2. ``adapters/db/tables.py`` is the single DDL source: this
revision renders the same schema against the target dialect (03 §4: Postgres
and SQLite stay in lockstep; one definition, two dialects).

CHECK literals mirror the domain enums (03 §1): ``CaseStatus``,
``Severity`` — the invariant suite introspects them to keep both in
lockstep. Timestamps are epoch seconds: ``BIGINT`` on Postgres (avoids the
int4 2038 overflow), rendered ``INTEGER`` on SQLite where the two are
identical.

Revision ID: 0001
Revises:
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# 03 §2: literals from the domain enums (03 §1 lockstep).
ROLES = ("viewer", "analyst", "approver", "admin")  # 03 §2.1 (domain Role lands in WP-103)
ORIGINS = ("alert", "ticket")  # 03 §2.2
SEVERITIES = ("P1", "P2", "P3", "P4")  # domain.severity.Severity
STATUSES = ("open", "triaged", "in_progress", "human_owned", "resolved", "reopened")  # domain.lifecycle.CaseStatus


def upgrade() -> None:
    # -- users (03 §2.1) ----------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("is_active", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
        sa.Column("last_login_ts", sa.BigInteger),
    )

    # -- role_assignments (03 §2.1) -------------------------------------------
    op.create_table(
        "role_assignments",
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("role", sa.Text, nullable=False, primary_key=True),
        sa.Column("scope", sa.Text, nullable=False, server_default=sa.text("'*'")),
        sa.Column("granted_by", sa.Text, sa.ForeignKey("users.id")),
        sa.Column("granted_ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "role IN ('viewer', 'analyst', 'approver', 'admin')", name="ck_role_assignments_role"
        ),
    )

    # -- case_groups (03 §2.2) -------------------------------------------------
    op.create_table(
        "case_groups",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("opened_ts", sa.BigInteger, nullable=False),
        sa.Column("closed_ts", sa.BigInteger),
        sa.Column("rule", sa.Text, nullable=False),
        sa.Column("member_count", sa.Integer, nullable=False, server_default=sa.text("0")),
    )

    # -- cases (03 §2.2) --------------------------------------------------------
    op.create_table(
        "cases",
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
        sa.Column("opened_ts", sa.BigInteger, nullable=False),
        sa.Column("last_alert_ts", sa.BigInteger, nullable=False),
        sa.Column("resolved_ts", sa.BigInteger),
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.CheckConstraint(
            "origin IN ('alert', 'ticket')", name="ck_cases_origin"
        ),
        sa.CheckConstraint(
            "severity IN ('P1', 'P2', 'P3', 'P4')", name="ck_cases_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'triaged', 'in_progress', 'human_owned', 'resolved', 'reopened')",
            name="ck_cases_status",
        ),
    )
    # INV-02: one unresolved case per fingerprint per tenant (03 §2.2).
    op.create_index(
        "ux_cases_unresolved",
        "cases",
        ["tenant_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
        sqlite_where=sa.text("status <> 'resolved'"),
    )
    op.create_index("ix_cases_status", "cases", ["tenant_id", "status", "opened_ts"])
    op.create_index("ix_cases_external", "cases", ["tenant_id", "external_table", "external_id"])

    # -- alerts (03 §2.2) -------------------------------------------------------
    op.create_table(
        "alerts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("adapter", sa.Text, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("payload_ref", sa.Text, nullable=False),
        sa.Column("received_ts", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_alerts_case", "alerts", ["tenant_id", "case_id"])

    # -- leases (03 §2.5): one global lock table — deliberately no tenant_id ---
    op.create_table(
        "leases",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("until_ts", sa.BigInteger, nullable=False),
    )

    # -- blobs + tenant_keys (03 §3) ---------------------------------------------
    op.create_table(
        "blobs",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("data", sa.LargeBinary, nullable=False),
    )
    op.create_table(
        "tenant_keys",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("wrapped_key", sa.LargeBinary, nullable=False),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_case", table_name="alerts")
    op.drop_index("ix_cases_external", table_name="cases")
    op.drop_index("ix_cases_status", table_name="cases")
    op.drop_index("ux_cases_unresolved", table_name="cases")
    op.drop_table("tenant_keys")
    op.drop_table("blobs")
    op.drop_table("leases")
    op.drop_table("alerts")
    op.drop_table("cases")
    op.drop_table("case_groups")
    op.drop_table("role_assignments")
    op.drop_table("users")
