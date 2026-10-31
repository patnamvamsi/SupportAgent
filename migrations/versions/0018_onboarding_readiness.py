"""0018_onboarding_readiness: the system's last readiness report (ADR-0015 §5, WP-O6b).

One row per tenant (an instance supports one system, decision 1): the
report as ``supportagent onboard readiness`` wrote it. Triage routes only
the labels of ready classes, and RCA marks a claim for a cause whose class
isn't ready as not sendable; ``/ui/onboarding/readiness`` shows why.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_readiness",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("report_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_readiness")
