"""0010_feedback: the three feedback moments (ARCH §8.3, 03 §2; WP-109).

One row per ``approval`` (the approver's reason), ``diff`` (a human change
paired with what the agent proposed or decided) and ``outcome`` (the final
labels at resolution). Each is also a ``feedback`` event on the case's
trace, so replay shows feedback in context. This table is the queryable
input to the learning loop (ARCH §10). 03 numbered it 0102; it ships as 0010
because v0.1 migrations continue the sequence.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("case_id", sa.Text, sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint("kind IN ('approval','diff','outcome')", name="ck_feedback_kind"),
    )
    op.create_index("ix_feedback_case", "feedback", ["tenant_id", "case_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_feedback_case", table_name="feedback")
    op.drop_table("feedback")
