"""Sync state (ARCH §8.1): the dedup set and the per-(tenant, table) cursor.

``SyncRepo`` is a thin, typed row accessor over the ``sync_seen`` and
``sync_cursor`` tables (migration 0004). It takes the active
``SqlUnitOfWork`` and runs on its connection, so it participates in the unit
of work's transaction (commit-on-demand, roll-back otherwise).

The read methods (``get_cursor`` / ``is_seen`` / ``max_seen_mod_count``) are
used inside a read-only UoW block; the write methods (``mark_seen`` /
``set_cursor``) inside the commit block. The sync service keeps them in
separate transactions so no ``BEGIN IMMEDIATE`` write lock is held across a
ServiceNow round-trip (ARCH §8.1: cursor advances + commits per page, exactly
as the outbox keeps claim and outcome in separate transactions). The dedup +
cursor invariants are enforced by that per-page transaction, not by a DB
trigger; this repo only moves rows. ``mark_seen`` is idempotent (check-then
insert) because a full page re-fetched across the 2-minute overlap re-inserts
rows that are already present.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from supportagent.adapters.db.tables import sync_cursor, sync_seen
from supportagent.adapters.db.uow import SqlUnitOfWork


class SyncRepo:
    """Typed access to the ``sync_seen`` / ``sync_cursor`` tables (thin)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    # --- sync_cursor ---------------------------------------------------------

    def get_cursor(self, tenant_id: str, table_name: str) -> datetime | None:
        """The committed poll cursor for this (tenant, table), or ``None``."""
        row = (
            self._uow.connection.execute(
                sa.select(sync_cursor).where(
                    sync_cursor.c.tenant_id == tenant_id,
                    sync_cursor.c.table_name == table_name,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return datetime.fromisoformat(str(row["cursor_iso"]))

    def set_cursor(self, tenant_id: str, table_name: str, cursor: datetime) -> None:
        """Upsert the committed poll cursor for this (tenant, table)."""
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=UTC)
        cursor_iso = cursor.astimezone(UTC).isoformat()
        existing = self._uow.connection.execute(
            sa.select(sync_cursor.c.tenant_id).where(
                sync_cursor.c.tenant_id == tenant_id,
                sync_cursor.c.table_name == table_name,
            )
        ).first()
        if existing is None:
            self._uow.connection.execute(
                sa.insert(sync_cursor).values(
                    tenant_id=tenant_id, table_name=table_name, cursor_iso=cursor_iso
                )
            )
        else:
            self._uow.connection.execute(
                sa.update(sync_cursor)
                .where(
                    sync_cursor.c.tenant_id == tenant_id,
                    sync_cursor.c.table_name == table_name,
                )
                .values(cursor_iso=cursor_iso)
            )

    # --- sync_seen -----------------------------------------------------------

    def is_seen(self, tenant_id: str, table_name: str, sys_id: str, sys_mod_count: int) -> bool:
        """Whether this exact record version was already delivered (read-only)."""
        row = self._uow.connection.execute(
            sa.select(sync_seen.c.tenant_id).where(
                sync_seen.c.tenant_id == tenant_id,
                sync_seen.c.table_name == table_name,
                sync_seen.c.sys_id == sys_id,
                sync_seen.c.sys_mod_count == sys_mod_count,
            )
        ).first()
        return row is not None

    def mark_seen(self, tenant_id: str, table_name: str, sys_id: str, sys_mod_count: int) -> None:
        """Record ``(sys_id, sys_mod_count)`` as seen (idempotent upsert)."""
        existing = self._uow.connection.execute(
            sa.select(sync_seen.c.tenant_id).where(
                sync_seen.c.tenant_id == tenant_id,
                sync_seen.c.table_name == table_name,
                sync_seen.c.sys_id == sys_id,
                sync_seen.c.sys_mod_count == sys_mod_count,
            )
        ).first()
        if existing is not None:
            return
        self._uow.connection.execute(
            sa.insert(sync_seen).values(
                tenant_id=tenant_id,
                table_name=table_name,
                sys_id=sys_id,
                sys_mod_count=sys_mod_count,
                seen_ts=0,
            )
        )

    def max_seen_mod_count(self, tenant_id: str, table_name: str, sys_id: str) -> int:
        """The highest ``sys_mod_count`` already seen for this record, or ``0``.

        The sync service passes this as ``history(after_mod_count=...)`` so a
        re-pulled record only yields changes newer than the last delivered
        version (ARCH §8.1).
        """
        row = self._uow.connection.execute(
            sa.select(sa.func.max(sync_seen.c.sys_mod_count)).where(
                sync_seen.c.tenant_id == tenant_id,
                sync_seen.c.table_name == table_name,
                sync_seen.c.sys_id == sys_id,
            )
        ).scalar()
        return int(row) if row is not None else 0


__all__ = ["SyncRepo"]
