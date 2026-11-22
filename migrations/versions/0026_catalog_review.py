"""0026_catalog_review: the service catalog drafted and accepted in the instance (ADR-0023 §2).

One row per tenant: the entries the ``catalog`` onboarding step drafted from
the graph under review, and the services a person accepted, each with the
paging integration they gave. Accepted services join the configured catalog
without a restart (WP-223).

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_review",
        sa.Column("tenant_id", sa.Text, primary_key=True),
        sa.Column("review_json", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("catalog_review")
