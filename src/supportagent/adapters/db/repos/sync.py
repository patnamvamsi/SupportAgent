"""Sync state (ARCH §8.1, 03 §2.5): the dedup set and the per-(tenant, table) cursor.

``SyncRepo`` is a thin, typed row accessor over ``sync_seen`` and
``sync_cursors`` (migration 0004). It runs on the active ``SqlUnitOfWork``'s
connection, so it participates in that transaction.

The sync service keeps reads and writes in separate transactions so no
``BEGIN IMMEDIATE`` write lock is held across a ServiceNow round-trip; the
cursor and the page's ``sync_seen`` rows commit together. ``mark_seen`` is an
idempotent upsert because the 2-minute overlap re-fetches rows already present.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

import sqlalchemy as sa

from supportagent.adapters.db.tables import sync_cursors, sync_journal_seen, sync_seen
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.db.upsert import upsert_stmt


class SyncRepo:
    """Typed access to the ``sync_seen`` / ``sync_cursors`` tables (thin)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    # --- sync_cursors --------------------------------------------------------

    def get_cursor(self, tenant_id: str, table_name: str) -> datetime | None:
        """The committed poll cursor for this (tenant, table), or ``None``."""
        ts = self._uow.connection.execute(
            sa.select(sync_cursors.c.cursor_ts).where(
                sync_cursors.c.tenant_id == tenant_id,
                sync_cursors.c.table_name == table_name,
            )
        ).scalar_one_or_none()
        return None if ts is None else datetime.fromtimestamp(int(ts), tz=UTC)

    def set_cursor(self, tenant_id: str, table_name: str, cursor: datetime) -> None:
        """Advance the committed cursor (never moves it backwards)."""
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=UTC)
        ts = int(cursor.timestamp())
        self._uow.connection.execute(
            upsert_stmt(
                self._uow.engine,
                sync_cursors,
                {"tenant_id": tenant_id, "table_name": table_name, "cursor_ts": ts},
                ["tenant_id", "table_name"],
                {"cursor_ts": ts},
                where=sync_cursors.c.cursor_ts < ts,
            )
        )

    # --- sync_seen -----------------------------------------------------------

    def is_seen(self, tenant_id: str, sys_id: str, sys_mod_count: int) -> bool:
        """Whether this exact record version was already delivered."""
        row = self._uow.connection.execute(
            sa.select(sync_seen.c.tenant_id).where(
                sync_seen.c.tenant_id == tenant_id,
                sync_seen.c.sys_id == sys_id,
                sync_seen.c.sys_mod_count == sys_mod_count,
            )
        ).first()
        return row is not None

    def mark_seen(self, tenant_id: str, sys_id: str, sys_mod_count: int, *, now_ts: int) -> None:
        """Record ``(sys_id, sys_mod_count)`` as processed (idempotent)."""
        self._uow.connection.execute(
            upsert_stmt(
                self._uow.engine,
                sync_seen,
                {
                    "tenant_id": tenant_id,
                    "sys_id": sys_id,
                    "sys_mod_count": sys_mod_count,
                    "processed_ts": now_ts,
                },
                ["tenant_id", "sys_id", "sys_mod_count"],
                {"processed_ts": sync_seen.c.processed_ts},
            )
        )

    def journal_seen(self, tenant_id: str, entry_ids: Collection[str]) -> set[str]:
        """BL-013: which of these journal entries were already delivered."""
        if not entry_ids:
            return set()
        rows = self._uow.connection.execute(
            sa.select(sync_journal_seen.c.entry_id).where(
                sync_journal_seen.c.tenant_id == tenant_id,
                sync_journal_seen.c.entry_id.in_(list(entry_ids)),
            )
        )
        return {str(r[0]) for r in rows}

    def mark_journal_seen(self, tenant_id: str, entry_ids: Collection[str], *, now_ts: int) -> None:
        for entry_id in entry_ids:
            self._uow.connection.execute(
                upsert_stmt(
                    self._uow.engine,
                    sync_journal_seen,
                    {"tenant_id": tenant_id, "entry_id": entry_id, "processed_ts": now_ts},
                    ["tenant_id", "entry_id"],
                    {"processed_ts": sync_journal_seen.c.processed_ts},
                )
            )

    def max_seen_mod_count(self, tenant_id: str, sys_id: str) -> int | None:
        """The highest ``sys_mod_count`` already processed for this record.

        ``None`` means never seen. That is distinct from ``0``: a new
        ServiceNow record has ``sys_mod_count = 0``, so "seen at 0" must not be
        mistaken for first sight.
        """
        row = self._uow.connection.execute(
            sa.select(sa.func.max(sync_seen.c.sys_mod_count)).where(
                sync_seen.c.tenant_id == tenant_id,
                sync_seen.c.sys_id == sys_id,
            )
        ).scalar()
        return None if row is None else int(row)


__all__ = ["SyncRepo"]
