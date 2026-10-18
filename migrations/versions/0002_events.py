"""0002_events: the append-only event log (03 §2.4, INV-03).

One table — ``events`` — with the two tenant-scoped indexes named in
03 §2.4 (``ix_events_trace``, ``ix_events_kind``) and the
``trg_events_append_only`` trigger that makes the log append-only at the
database level: ``BEFORE UPDATE`` aborts with ``INV-03: events are
append-only`` on both engines. ``DELETE`` is reserved for the retention
sweep (03 §2.4); this revision adds no other write path.

``adapters/db/tables.py`` is the single DDL source: this revision renders
the same schema against the target dialect (03 §4: one definition, two
dialects). CHECK literals are generated from the domain enums (03 §1):
``EventKind`` (exactly 20, ARCH §15) and the ``Event.status`` literal set
(01 §4). ``payload_enc`` holds the AES-256-GCM envelope of the canonical
JSON payload (03 §2.4, §2.6); ``ts_ms`` is epoch milliseconds.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from supportagent.domain.events import EventKind

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# 03 §1 lockstep: literals from the domain (03 §2.4, 01 §4).
KINDS = tuple(k.value for k in EventKind)
STATUSES = ("ok", "error", "skipped")

APPEND_ONLY_MSG = "INV-03: events are append-only"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    op.create_table(
        "events",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.Column("span_id", sa.Text, nullable=False),
        sa.Column("parent_span_id", sa.Text),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("tokens_in", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text, nullable=False),
        # AES-256-GCM envelope of the canonical JSON payload (03 §2.4, §2.6).
        sa.Column("payload_enc", sa.LargeBinary, nullable=False),
        # Epoch milliseconds (03 §1); 64-bit so Postgres INT4 cannot overflow.
        sa.Column("ts_ms", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            f"kind IN ({', '.join(repr(k) for k in KINDS)})", name="ck_events_kind"
        ),
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in STATUSES)})", name="ck_events_status"
        ),
    )
    # 03 §2.4: tenant-scoped indexes.
    op.create_index("ix_events_trace", "events", ["tenant_id", "trace_id", "ts_ms"])
    op.create_index("ix_events_kind", "events", ["tenant_id", "kind", "ts_ms"])

    # INV-03: append-only at the database level (UPDATE → ABORT, 03 §2.4).
    if dialect == "sqlite":
        op.execute(
            f"CREATE TRIGGER trg_events_append_only "
            f"BEFORE UPDATE ON events "
            f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MSG}'); END;"
        )
    else:
        # Postgres: RAISE(ABORT) is SQLite-only; the plpgsql equivalent is a
        # BEFORE UPDATE trigger function that raises the same message.
        op.execute(
            "CREATE FUNCTION sa_events_append_only_guard() RETURNS trigger AS $$ "
            f"BEGIN RAISE EXCEPTION '{APPEND_ONLY_MSG}'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_events_append_only "
            "BEFORE UPDATE ON events "
            "FOR EACH ROW EXECUTE FUNCTION sa_events_append_only_guard()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_events_append_only")
    else:
        # Postgres: DROP TRIGGER is table-scoped (needs the table name).
        op.execute("DROP TRIGGER IF EXISTS trg_events_append_only ON events")
        op.execute("DROP FUNCTION IF EXISTS sa_events_append_only_guard()")
    op.drop_index("ix_events_kind", table_name="events")
    op.drop_index("ix_events_trace", table_name="events")
    op.drop_table("events")
