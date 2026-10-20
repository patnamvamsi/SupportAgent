"""0007_comms_drafts: cadence updates kept as drafts for a human owner (WP-102).

ARCH §8.2: while a case is ``human_owned`` the agent sends nothing. A due
plan update is rendered as a **draft for the owner**, with no outbox row,
so comms debt is visible on the case instead of silently dropped. One draft
per (plan, trigger, slot), so re-running the cadence never duplicates it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comms_drafts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("plan_id", sa.Text, sa.ForeignKey("comms_plans.id"), nullable=False),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("interval_slot", sa.Integer, nullable=False),
        sa.Column("body_json", sa.Text, nullable=False),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("plan_id", "trigger", "interval_slot", name="uq_comms_drafts_slot"),
    )
    op.create_index("ix_comms_drafts_case", "comms_drafts", ["tenant_id", "case_id"])


def downgrade() -> None:
    op.drop_index("ix_comms_drafts_case", table_name="comms_drafts")
    op.drop_table("comms_drafts")
