"""0016_graph_review: the system graph a person reviews (ADR-0015 §3, WP-O3b).

``graph_facts`` holds each node and edge onboarding proposed, with its
sources (and quotes, for facts read from documentation), its layer of trust,
and its review status: ``proposed``, ``confirmed`` or ``rejected``. A rebuild
keeps every review decision; a fact the latest build no longer finds is kept
with ``present = 0`` so its review isn't lost. ``graph_questions`` holds the
conflicts between sources, settled by choosing one of their options.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_facts",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("fact", sa.Text, nullable=False),  # node | edge
        sa.Column("src", sa.Text, nullable=False),  # the node id, or the edge's source
        sa.Column("kind", sa.Text, nullable=False),  # the node kind, or the edge kind
        sa.Column("dst", sa.Text),
        sa.Column("tech_json", sa.Text, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("sources_json", sa.Text, nullable=False),
        sa.Column("layer", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("present", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("reviewed_by", sa.Text),
        sa.Column("reviewed_ts", sa.BigInteger),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint("fact IN ('node', 'edge')", name="ck_graph_facts_fact"),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_graph_facts_status"
        ),
    )
    op.create_index("ix_graph_facts_review", "graph_facts", ["tenant_id", "status", "layer"])
    op.create_table(
        "graph_questions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("about", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("sources_json", sa.Text, nullable=False),
        sa.Column("options_json", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("choice", sa.Integer),
        sa.Column("resolved_by", sa.Text),
        sa.Column("resolved_ts", sa.BigInteger),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_graph_questions_status"),
    )


def downgrade() -> None:
    op.drop_table("graph_questions")
    op.drop_index("ix_graph_facts_review", table_name="graph_facts")
    op.drop_table("graph_facts")
