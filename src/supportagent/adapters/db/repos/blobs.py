"""Blob + wrapped-key rows (03 §3): raw payloads and the wrapped data keys.

``SqlKeyStore`` is the database side of the crypto boundary: it satisfies
the ``KeyStore`` protocol declared in ``supportagent.adapters.crypto.aead``
structurally — the two packages never import each other (01 §2,
adapters-independent), so ``Aead`` stays free of SQL and the repos stay
free of cryptography.
"""

from __future__ import annotations

import sqlalchemy as sa

from supportagent.adapters.db.tables import blobs, tenant_keys
from supportagent.adapters.db.uow import SqlUnitOfWork
from supportagent.adapters.db.upsert import upsert_stmt
from supportagent.domain.errors import NotFound


class BlobRepo:
    """Raw payload storage keyed by (tenant, key) (03 §3)."""

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def put(self, *, tenant_id: str, key: str, data: bytes) -> None:
        """Store or replace a raw payload (03 §3: payloads never inline in rows)."""
        self._uow.connection.execute(
            upsert_stmt(
                self._uow.engine,
                blobs,
                {"key": key, "tenant_id": tenant_id, "data": data},
                [blobs.c.key],
                {"data": data},
            )
        )

    def get(self, *, tenant_id: str, key: str) -> bytes:
        """Load a raw payload; raises ``NotFound`` for a missing key (03 §3)."""
        row = self._uow.connection.execute(
            sa.select(blobs.c.data).where(blobs.c.key == key, blobs.c.tenant_id == tenant_id)
        ).first()
        if row is None:
            raise NotFound(  # noqa: TRY003
                f"blob not found: tenant={tenant_id!r} key={key!r}"
            )
        return bytes(row[0])  # DBAPI may surface bytes as memoryview (psycopg)


class SqlKeyStore:
    """Database-backed wrapped tenant data keys (03 §3).

    Structural implementation of the ``KeyStore`` protocol
    (``supportagent.adapters.crypto.aead``); the wrapped key is opaque
    bytes — its layout belongs to the crypto package.
    """

    def __init__(self, uow: SqlUnitOfWork) -> None:
        self._uow = uow

    def get_wrapped_key(self, tenant_id: str) -> bytes | None:
        """The tenant's wrapped data key, or ``None`` if it does not exist."""
        row = self._uow.connection.execute(
            sa.select(tenant_keys.c.wrapped_key).where(tenant_keys.c.tenant_id == tenant_id)
        ).first()
        return row[0] if row is not None else None

    def put_wrapped_key(self, tenant_id: str, wrapped: bytes, created_ts: int) -> None:
        """Store or refresh the tenant's wrapped data key (03 §3)."""
        self._uow.connection.execute(
            upsert_stmt(
                self._uow.engine,
                tenant_keys,
                {
                    "tenant_id": tenant_id,
                    "wrapped_key": wrapped,
                    "created_ts": created_ts,
                },
                [tenant_keys.c.tenant_id],
                {"wrapped_key": wrapped},
            )
        )

    def delete_wrapped_key(self, tenant_id: str) -> None:
        """Delete the wrapped key — the crypto-shred primitive (03 §3)."""
        self._uow.connection.execute(
            sa.delete(tenant_keys).where(tenant_keys.c.tenant_id == tenant_id)
        )
