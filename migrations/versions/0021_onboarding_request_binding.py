"""0021_onboarding_request_binding: a request can be ``bound``, with its probes (ADR-0019, WP-O5b).

Binding an approved request's credential runs the contract's probes; the
verdict and each probe's result land on the request (``verification_json``).
The credential itself goes to the credential vault, never here. A request
whose credential was proven read-only is ``bound``.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_NAME = "ck_onboarding_requests_status"


def _statuses(values: tuple[str, ...]) -> str:
    return f"status IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    with op.batch_alter_table("onboarding_requests", recreate="auto") as batch:
        batch.add_column(sa.Column("verification_json", sa.Text))
        batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _statuses(("open", "approved", "declined", "bound")))


def downgrade() -> None:
    op.execute("UPDATE onboarding_requests SET status = 'approved' WHERE status = 'bound'")
    with op.batch_alter_table("onboarding_requests", recreate="auto") as batch:
        batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _statuses(("open", "approved", "declined")))
        batch.drop_column("verification_json")
