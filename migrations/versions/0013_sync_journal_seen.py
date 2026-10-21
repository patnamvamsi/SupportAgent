"""0013_sync_journal_seen: journal entries already delivered (BL-013).

The poll re-reads a 2-minute overlap window, so a ``sys_journal_field``
entry can come back on two polls. ``sync_seen`` dedups record versions;
this table dedups journal entries by their ``sys_id``. The retention sweep
ages rows out with the event log.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_journal_seen",
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("entry_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("processed_ts", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_journal_seen")
