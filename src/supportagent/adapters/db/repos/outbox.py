"""Outbox rows (10-wp): the INV-01 *execution* write point.

``OutboxRepo`` is a thin, typed row accessor over the ``outbox`` table
(03 §4). It takes the active ``SqlUnitOfWork`` and runs on its connection,
so it participates in the unit of work's transaction (commit-on-demand,
roll-back otherwise).

INV-01 is enforced by the database trigger ``trg_outbox_inv01``
(migration 0003), not here: when the trigger rejects an insert it raises a
driver error whose message is prefixed ``INV-01:``. This repo translates
that one class of driver error into the domain error
``AuthorizationDenied`` (01 §6) so application code sees a typed result
instead of a dialect-specific DBAPI exception. Every other driver error is
re-raised unchanged (10-wp: do not mask real failures).
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from supportagent.adapters.db.errors import to_domain_error
from supportagent.adapters.db.tables import outbox
from supportagent.adapters.db.uow import SqlUnitOfWork


class OutboxRepo:
    """Typed access to the ``outbox`` table (10-wp: thin, no orchestration)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    # 12 keyword params == the full outbox row (10-wp: thin repository, one
    # param per column). PLR0913 suppressed: the signature is the row contract.
    def insert(  # noqa: PLR0913
        self,
        *,
        id: str,
        tenant_id: str,
        case_id: str,
        action_kind: str,
        scope: str,
        payload_json: str,
        payload_hash: str,
        idempotency_key: str,
        approval_id: str | None = None,
        policy_grant_id: str | None = None,
        comms_plan_id: str | None = None,
        trace_id: str,
    ) -> None:
        """Insert a new outbox row (10-wp).

        ``status`` is omitted so the server default (``'pending'``) applies.
        The INV-01 trigger may reject the insert; that surfaces as
        ``AuthorizationDenied`` (01 §6).
        """
        values = {
            "id": id,
            "tenant_id": tenant_id,
            "case_id": case_id,
            "action_kind": action_kind,
            "scope": scope,
            "payload_json": payload_json,
            "payload_hash": payload_hash,
            "idempotency_key": idempotency_key,
            "approval_id": approval_id,
            "policy_grant_id": policy_grant_id,
            "comms_plan_id": comms_plan_id,
            "trace_id": trace_id,
        }
        try:
            self._uow.connection.execute(sa.insert(outbox).values(**values))
        except sa.exc.DBAPIError as exc:
            denied = to_domain_error(exc)
            if denied is not None:
                raise denied from exc
            raise

    def get(self, outbox_id: str) -> dict[str, Any] | None:
        """Load one outbox row as a mapping, or ``None``."""
        row = (
            self._uow.connection.execute(sa.select(outbox).where(outbox.c.id == outbox_id))
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def get_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        """Load the row with a UNIQUE idempotency key, or ``None`` (INV-04)."""
        row = (
            self._uow.connection.execute(
                sa.select(outbox).where(outbox.c.idempotency_key == idempotency_key)
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None

    def claim_next(self, *, now_ts: int) -> dict[str, Any] | None:
        """Claim the next due row (ARCH L498): exclusive, rowcount-checked CAS.

        A row is *due* when ``status='pending'`` and
        ``next_attempt_ts IS NULL OR <= now_ts`` (01 §7.5). The SELECT picks the
        oldest due row; the UPDATE re-checks ``status='pending'`` and is guarded
        by a rowcount of exactly 1, so two workers racing on the same row cannot
        both claim it (the loser sees ``rowcount == 0`` and claims nothing).
        Both statements run inside the caller's transaction.
        """
        due = outbox.c.status == "pending"
        conn = self._uow.connection
        row_id = conn.execute(
            sa.select(outbox.c.id)
            .where(
                due,
                sa.or_(
                    outbox.c.next_attempt_ts.is_(None),
                    outbox.c.next_attempt_ts <= now_ts,
                ),
            )
            .order_by(outbox.c.id)
            .limit(1)
        ).scalar_one_or_none()
        if row_id is None:
            return None
        result = conn.execute(
            sa.update(outbox)
            .where(outbox.c.id == row_id, outbox.c.status == "pending")
            .values(status="claimed", claimed_at_ts=now_ts)
            .execution_options(preserve_rowcount=True)
        )
        if int(result.rowcount) != 1:
            return None
        return self.get(row_id)

    def mark_done(self, outbox_id: str, *, external_ref: str | None, now_ts: int) -> None:
        """Record a completed send (03 §2.3: trigger-safe bookkeeping only)."""
        self._uow.connection.execute(
            sa.update(outbox)
            .where(outbox.c.id == outbox_id)
            .values(status="done", done_at_ts=now_ts, external_ref=external_ref)
        )

    def mark_failed(self, outbox_id: str, *, error: str, retry_at_ts: int | None) -> None:
        """Record a failed attempt: one attempt consumed, then failed or re-queued.

        ``retry_at_ts is None`` → terminal ``failed``; otherwise the row returns
        to ``pending`` with the retry deadline (01 §7.5 backoff, set by caller).
        """
        if retry_at_ts is None:
            values: dict[str, Any] = {
                "status": "failed",
                "attempts": outbox.c.attempts + 1,
                "last_error": error,
            }
        else:
            values = {
                "status": "pending",
                "attempts": outbox.c.attempts + 1,
                "last_error": error,
                "next_attempt_ts": retry_at_ts,
            }
        self._uow.connection.execute(
            sa.update(outbox).where(outbox.c.id == outbox_id).values(**values)
        )


__all__ = ["OutboxRepo"]
