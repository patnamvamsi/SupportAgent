"""0004_sync: ServiceNow sync state (ARCH §8.1, 01 §7.5, 02 §6).

Two tables backing the sync service (``app.sync``):

* ``sync_seen`` — the dedup set. A ``(tenant, table, sys_id, sys_mod_count)``
  row means "this version of this record has already been handed to the
  inbound handler". The sync service inserts one row per processed record and
  skips any it has already seen, so the 2-minute poll overlap never re-delivers
  the same change (ARCH §8.1: dedup on ``(sys_id, sys_mod_count)``).
* ``sync_cursor`` — the per-``(tenant, table)`` poll cursor (aware-UTC ISO-8601
  in ``cursor_iso``). It advances one page at a time and is committed together
  with that page's ``sync_seen`` rows, so a crash mid-batch resumes from the
  last committed page (ARCH §8.1: cursor advances + commits per page).

``adapters/db/tables.py`` is the single DDL source; this revision renders the
same schema against the target dialect (03 §4: one definition, two dialects).
No CHECK/trigger is needed: the dedup + cursor invariants are enforced by the
service's transaction (commit-per-page), not by a DB trigger.

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
        "sync_seen",
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("table_name", sa.Text, nullable=False, primary_key=True),
        sa.Column("sys_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("sys_mod_count", sa.Integer, nullable=False, primary_key=True),
        sa.Column("seen_ts", sa.Integer, nullable=False),
    )
    op.create_table(
        "sync_cursor",
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("table_name", sa.Text, nullable=False, primary_key=True),
        sa.Column("cursor_iso", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_cursor")
    op.drop_table("sync_seen")

