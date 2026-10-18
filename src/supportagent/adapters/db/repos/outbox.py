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


__all__ = ["OutboxRepo"]
