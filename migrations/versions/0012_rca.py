"""0012_rca: one RCA-lite artifact per case (ARCH §5; WP-110).

``outcome`` is the categorical ``reduce()`` result; ``artifact_json`` holds
the plan, the per-step evidence, the claims and their entailment tags. One
artifact per case (one RCA per storm group: only the lead runs). 11-wp named
it 0103; it ships as 0012 because v0.1 migrations continue the sequence.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rca_artifacts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("artifact_json", sa.Text, nullable=False),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "outcome IN ('SUPPORTED','CONSISTENT_UNCONFIRMED','INCONCLUSIVE')",
            name="ck_rca_artifacts_outcome",
        ),
    )
    op.create_index("ux_rca_artifacts_case", "rca_artifacts", ["tenant_id", "case_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_rca_artifacts_case", table_name="rca_artifacts")
    op.drop_table("rca_artifacts")
