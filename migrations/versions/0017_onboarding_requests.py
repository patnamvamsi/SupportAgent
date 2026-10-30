"""0017_onboarding_requests: the access a system's onboarding asks for (ADR-0015 §4, WP-O5).

One row per request: a plugin's least-privilege contract on one target, its
scope (the contract's grant, verbatim), why it is asked for (the incident
classes it unlocks and their share of the history's weight) and its review
status: ``open``, ``approved`` (a person will provide the credential) or
``declined``. A re-plan keeps decisions; a request the latest plan no longer
makes is kept with ``present = 0``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_requests",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("plugin", sa.Text, nullable=False),
        sa.Column("access", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=False),
        sa.Column("detail_json", sa.Text, nullable=False),
        sa.Column("share", sa.Float, nullable=False),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("present", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("decided_by", sa.Text),
        sa.Column("decided_ts", sa.BigInteger),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'approved', 'declined')", name="ck_onboarding_requests_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding_requests")
