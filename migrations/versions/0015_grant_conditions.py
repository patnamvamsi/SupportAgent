"""0015_grant_conditions: conditions on a policy grant (ADR-0014 part 2, BL-054).

An SOP auto-mode grant carries conditions the gate checks on every proposal
it would cover (positive checks, services, a daily cap). NULL for every
existing grant: an unconditioned grant behaves as before, and an
unconditioned grant on an ``sop:`` scope covers nothing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A plain ADD COLUMN on both engines: no table rebuild under the INV-01
    # trigger and the outbox foreign key that read this table.
    op.add_column("policy_grants", sa.Column("conditions_json", sa.Text))


def downgrade() -> None:
    # A plain DROP COLUMN too (SQLite >= 3.35): a batch rebuild would rename
    # the table under trg_outbox_inv01, which SQLite refuses.
    op.drop_column("policy_grants", "conditions_json")
