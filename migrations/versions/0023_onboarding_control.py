"""0023_onboarding_control: onboarding driven from the instance (ADR-0023 §2, WP-223).

One row per tenant: the system manifest a person edits on ``/ui/onboarding/system``,
who changed it last, and the run requests and run state of the
``onboard.autopilot`` worker job ("Run now" sets a request; the job clears it).

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_control",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("control_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_control")
