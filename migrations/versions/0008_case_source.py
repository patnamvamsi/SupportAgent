"""0008_case_source: the upstream source of a case (01 §7.8 rule c, WP-111).

Storm grouping joins a case to a group holding at least 5 cases from the
same upstream source. ``cases.source`` records ``AlertEvent.source`` at
ingest. It's nullable: cases opened before this migration have none and never
match rule (c).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN on both engines: no table rebuild, so the partial
    # unique index behind INV-02 (ux_cases_unresolved) is untouched.
    op.add_column("cases", sa.Column("source", sa.Text))


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.drop_column("source")
