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

    def claim_next(self, *, now_ts: int, lease_s: int) -> dict[str, Any] | None:
        """Claim the next due row (ARCH §6.2 L498, 01 §3): exclusive, rowcount-checked CAS.

        A row is *due* when it is ``pending`` with ``next_attempt_ts IS NULL OR
        <= now_ts`` (01 §7.5), or ``claimed`` by a worker whose claim is older
        than ``lease_s`` (that worker crashed between claim and outcome; the
        executor reconciles before re-sending, INV-04). Reclaiming a stale
        claim consumes one attempt, so a row that crashes its worker every time
        cannot loop forever.

        The UPDATE re-checks the observed ``status`` and ``claimed_at_ts`` and
        is guarded by a rowcount of exactly 1, so two workers racing on the
        same row cannot both claim it. The caller commits the claim *before*
        any external call, and passes the returned ``claimed_at_ts`` back to
        ``mark_done`` / ``mark_failed`` as the fencing token.
        """
        conn = self._uow.connection
        pending_due = sa.and_(
            outbox.c.status == "pending",
            sa.or_(outbox.c.next_attempt_ts.is_(None), outbox.c.next_attempt_ts <= now_ts),
        )
        stale_claim = sa.and_(
            outbox.c.status == "claimed",
            outbox.c.claimed_at_ts <= now_ts - lease_s,
        )
        found = conn.execute(
            sa.select(outbox.c.id, outbox.c.status, outbox.c.claimed_at_ts)
            .where(sa.or_(pending_due, stale_claim))
            .order_by(outbox.c.id)
            .limit(1)
        ).first()
        if found is None:
            return None
        row_id, status, claimed_at = found
        values: dict[str, Any] = {"status": "claimed", "claimed_at_ts": now_ts}
        if status == "claimed":
            values["attempts"] = outbox.c.attempts + 1
            values["last_error"] = "reclaimed after stale claim"
        claimed_match = (
            outbox.c.claimed_at_ts.is_(None)
            if claimed_at is None
            else outbox.c.claimed_at_ts == claimed_at
        )
        result = conn.execute(
            sa.update(outbox)
            .where(outbox.c.id == row_id, outbox.c.status == status, claimed_match)
            .values(**values)
            .execution_options(preserve_rowcount=True)
        )
        if int(result.rowcount) != 1:
            return None
        return self.get(row_id)

    def mark_done(
        self, outbox_id: str, *, claimed_at_ts: int, external_ref: str | None, now_ts: int
    ) -> bool:
        """Record a completed send, if this worker still holds the claim.

        Returns ``False`` when the claim was lost (reclaimed after ``lease_s``);
        the newer claimant then owns the row's outcome. Bookkeeping columns
        only (03 §2.3: trigger-safe).
        """
        return self._update_claimed(
            outbox_id,
            claimed_at_ts,
            {"status": "done", "done_at_ts": now_ts, "external_ref": external_ref},
        )

    def mark_failed(
        self,
        outbox_id: str,
        *,
        claimed_at_ts: int,
        error: str,
        retry_at_ts: int | None,
        consume_attempt: bool = True,
    ) -> bool:
        """Record a failed attempt: one attempt consumed, then failed or re-queued.

        ``retry_at_ts is None`` → terminal ``failed``; otherwise the row returns
        to ``pending`` with the retry deadline (01 §7.5 backoff, set by caller).
        ``consume_attempt=False`` is for a budget already spent by stale-claim
        reclaims. Fenced on the claim like ``mark_done``.
        """
        values: dict[str, Any] = {
            "status": "failed" if retry_at_ts is None else "pending",
            "last_error": error,
        }
        if consume_attempt:
            values["attempts"] = outbox.c.attempts + 1
        if retry_at_ts is not None:
            values["next_attempt_ts"] = retry_at_ts
        return self._update_claimed(outbox_id, claimed_at_ts, values)

    def _update_claimed(self, outbox_id: str, claimed_at_ts: int, values: dict[str, Any]) -> bool:
        result = self._uow.connection.execute(
            sa.update(outbox)
            .where(
                outbox.c.id == outbox_id,
                outbox.c.status == "claimed",
                outbox.c.claimed_at_ts == claimed_at_ts,
            )
            .values(**values)
            .execution_options(preserve_rowcount=True)
        )
        return int(result.rowcount) == 1


__all__ = ["OutboxRepo"]
