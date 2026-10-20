"""0009_webhooks: replay protection and the inbox for generic webhooks (WP-113).

- ``webhook_nonces``: each ``X-SA-Nonce`` is accepted once per source (ARCH §7
  replay protection). A nonce expires after twice the 5-minute window, and
  the receiver purges expired rows.
- ``webhook_inbox``: a verified, mapped ``AlertEvent``, **encrypted** with the
  tenant key (03 §3), waiting for the worker. The HTTP request returns at
  once instead of holding the sender while the LLM runs. ``processed_ts``
  marks it done.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_nonces",
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("source", sa.Text, nullable=False, primary_key=True),
        sa.Column("nonce", sa.Text, nullable=False, primary_key=True),
        sa.Column("expires_ts", sa.BigInteger, nullable=False),
    )
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("event_enc", sa.LargeBinary, nullable=False),
        sa.Column("received_ts", sa.BigInteger, nullable=False),
        sa.Column("processed_ts", sa.BigInteger),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_webhook_inbox_pending", "webhook_inbox", ["tenant_id", "processed_ts"])


def downgrade() -> None:
    op.drop_index("ix_webhook_inbox_pending", table_name="webhook_inbox")
    op.drop_table("webhook_inbox")
    op.drop_table("webhook_nonces")
