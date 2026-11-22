"""0024_component_summaries: what each component does (ADR-0023 §3, WP-222).

One row per tenant: each component's summary, written by onboarding from its
cited facts, with the digest of what the model was shown (an unchanged
component is never summarised again) and the inferred facts it proposed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "component_summaries",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("summaries_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("component_summaries")
