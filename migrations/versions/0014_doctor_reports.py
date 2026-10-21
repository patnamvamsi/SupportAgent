"""0014_doctor_reports: the last ``doctor`` report per tenant (BL-031, 02 §3).

``/ui/setup`` shows the deployment's doctor status. A doctor run makes
network calls, so the page reads the report the last run saved instead of
running checks per request.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doctor_reports",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("report_json", sa.Text, nullable=False),
        sa.Column("ran_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("doctor_reports")
