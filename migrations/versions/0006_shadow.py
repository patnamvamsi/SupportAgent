"""0006_shadow: the counterfactual record of shadow mode (03 §2.6, WP-014).

One ``shadow_records`` row per shadow case: when the alert arrived, when the
agent *would have* proposed (NULL on a safe default), what it would have
proposed (decision + ActionSpecs), and — once known — the human's final
classification, severity and first action, with ``agreement`` (NULL until the
human label is known). Numbered 0006 because 0005 is the WP-012 bootstrap-admin
index.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shadow_records",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("alert_received_ts", sa.BigInteger, nullable=False),
        sa.Column("first_proposal_ts", sa.BigInteger),
        sa.Column("proposed_json", sa.Text, nullable=False),
        sa.Column("human_classification", sa.Text),
        sa.Column("human_severity", sa.Text),
        sa.Column("human_first_action_ts", sa.BigInteger),
        sa.Column("human_action_json", sa.Text),
        sa.Column("agreement", sa.Integer),
        sa.UniqueConstraint("tenant_id", "case_id", name="uq_shadow_records_case"),
    )
    op.create_index(
        "ix_shadow_records_received", "shadow_records", ["tenant_id", "alert_received_ts"]
    )


def downgrade() -> None:
    op.drop_index("ix_shadow_records_received", table_name="shadow_records")
    op.drop_table("shadow_records")
