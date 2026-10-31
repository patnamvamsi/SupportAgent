"""0019_onboarding_status: the last onboarding run's steps and needs (ADR-0017, WP-A4).

One row per tenant: what ``supportagent onboard run`` found, step by step, and
what it is waiting on a person for, ranked by the incident weight each item
holds back. ``/ui/onboarding`` shows it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_status",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("status_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_status")
