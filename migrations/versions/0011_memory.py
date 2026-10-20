"""0011_memory: case memory, nodes / edges / kb_docs (ARCH §5.2, §5.3; WP-108).

- ``nodes``: one per remembered case (``kind = 'case'``, ``ref`` = case id,
  ``label`` = its JSON frontmatter: service, classification, severity,
  normalized signature, resolved flag) and one per normalized error
  signature (``kind = 'signature'``).
- ``edges``: derived only, never hand-maintained: ``has_signature``
  (case → signature), and ``similar`` between two cases with an equal
  normalized signature (ARCH §5.3).
- ``kb_docs``: each case's **redacted, normalized** text for BM25. Plain
  rows on both engines; scoring is computed in the application.

03 numbered memory 0101; it ships as 0011 because v0.1 migrations continue
the sequence.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nodes",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("tenant_id", "kind", "ref", name="uq_nodes_ref"),
    )
    op.create_table(
        "edges",
        sa.Column("src", sa.Text, sa.ForeignKey("nodes.id"), nullable=False, primary_key=True),
        sa.Column("dst", sa.Text, sa.ForeignKey("nodes.id"), nullable=False, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, primary_key=True),
        sa.Column("weight", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_ts", sa.BigInteger, nullable=False),
    )
    op.create_index("ix_edges_dst", "edges", ["tenant_id", "dst", "kind"])
    op.create_table(
        "kb_docs",
        sa.Column("tenant_id", sa.Text, nullable=False, primary_key=True),
        sa.Column("ref", sa.Text, nullable=False, primary_key=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("updated_ts", sa.BigInteger, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("kb_docs")
    op.drop_index("ix_edges_dst", table_name="edges")
    op.drop_table("edges")
    op.drop_table("nodes")
