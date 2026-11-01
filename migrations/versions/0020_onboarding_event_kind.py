"""0020_onboarding_event_kind: the event log admits ``onboarding`` (ADR-0017, WP-A6).

Onboarding's audit trail (a step, a plugin installed, an access or graph
decision, readiness, calibration, drift) goes to the append-only event log
like every other decision (INV-03), as one new kind whose payload names the
action. 0002 builds its constraint from ``EventKind``, so a fresh database
already admits it; this recreates the constraint on one made before.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op

from supportagent.domain.events import EventKind

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_NAME = "ck_events_kind"
#: 0002's INV-03 guard. SQLite can't alter a constraint, so the table is
#: rebuilt, and a rebuild drops its triggers: the guard is put back here.
_APPEND_ONLY_MSG = "INV-03: events are append-only"


def _check(kinds: tuple[str, ...]) -> str:
    return f"kind IN ({', '.join(repr(k) for k in kinds)})"


def _replace(kinds: tuple[str, ...]) -> None:
    with op.batch_alter_table("events", recreate="auto") as batch:
        batch.drop_constraint(_NAME, type_="check")
        batch.create_check_constraint(_NAME, _check(kinds))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_events_append_only")
        op.execute(
            "CREATE TRIGGER trg_events_append_only BEFORE UPDATE ON events "
            f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MSG}'); END;"
        )


def upgrade() -> None:
    _replace(tuple(k.value for k in EventKind))


def downgrade() -> None:
    _replace(tuple(k.value for k in EventKind if k is not EventKind.ONBOARDING))
