"""0004_sync: ServiceNow sync state (ARCH §8.1, 03 §2.5).

* ``sync_cursors`` — the per-``(tenant, table)`` poll cursor: the max
  ``sys_updated_on`` processed, epoch seconds. It advances one page at a time
  and commits together with that page's ``sync_seen`` rows, so a crash
  mid-batch resumes from the last committed page.
* ``sync_seen`` — the dedup set on ``(tenant, sys_id, sys_mod_count)``: this
  version of this record was already handed to the inbound handler, so the
  2-minute poll overlap never re-delivers it.
* ``automation_actors`` — v0.0 table, used from v0.1 (confirmed automation
  accounts whose changes are not human takeovers).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sync_cursors",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("table_name", sa.Text, nullable=False),
        sa.Column("cursor_ts", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "table_name"),
    )
    op.create_table(
        "sync_seen",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("sys_id", sa.Text, nullable=False),
        sa.Column("sys_mod_count", sa.Integer, nullable=False),
        sa.Column("processed_ts", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "sys_id", "sys_mod_count"),
    )
    op.create_table(
        "automation_actors",
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("sys_user", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("confirmed_by", sa.Text, sa.ForeignKey("users.id")),
        sa.Column("confirmed_ts", sa.Integer),
        sa.PrimaryKeyConstraint("tenant_id", "sys_user"),
    )


def downgrade() -> None:
    op.drop_table("automation_actors")
    op.drop_table("sync_seen")
    op.drop_table("sync_cursors")
