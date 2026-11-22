"""0025_detector_review: measured detectors, reviewed and evaluated in the instance.

ADR-0023 §2 and §4 (WP-223, WP-224). One row per tenant in each table:

- ``detector_review``: what the ``detectors`` onboarding step measured and
  proposed, which ones a person enabled (and whether the instance evaluates
  them or exports them as rule files), and what has been looked at;
- ``detector_watch``: each evaluated detector's state (pending since, fired at,
  last checked, last error), so a restart neither re-fires nor forgets one.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "detector_review",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("review_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "detector_watch",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("watch_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("detector_watch")
    op.drop_table("detector_review")
