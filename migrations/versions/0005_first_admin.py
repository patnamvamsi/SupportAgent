"""0005_first_admin: at most one bootstrap admin per tenant (03 §2.1, WP-012).

The first login with no admin becomes ``admin`` with ``granted_by`` NULL; every
later role change is made by an admin, so ``granted_by`` is set. The partial
unique index makes "first user becomes admin once" a database property: two
concurrent first logins cannot both insert a bootstrap admin row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_WHERE = "role = 'admin' AND granted_by IS NULL"


def upgrade() -> None:
    op.create_index(
        "ux_role_assignments_bootstrap_admin",
        "role_assignments",
        ["tenant_id"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    op.drop_index("ux_role_assignments_bootstrap_admin", table_name="role_assignments")
