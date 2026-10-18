"""AEAD envelope + crypto-shred (03 §3).

Per-tenant data keys (AES-256-GCM) are wrapped by the master key and stored
in ``tenant_keys``; raw alert payloads go to ``blobs``. Crypto-shred deletes
the tenant's wrapped data key, which must make every previously-encrypted
payload permanently unreadable — surfaced as a stable ``InvalidTag``.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.engine import Engine

from supportagent.adapters.crypto.aead import Aead
from supportagent.adapters.db.repos.blobs import BlobRepo
from supportagent.adapters.db.uow import SqlUnitOfWork


def _tenant_key_count(db: Engine, tenant_id: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM tenant_keys WHERE tenant_id = :t"),
                {"t": tenant_id},
            ).scalar_one()
        )


def test_aead_roundtrip(db: Engine, uow: SqlUnitOfWork, aead: Aead) -> None:
    """encrypt() then decrypt() returns the original bytes; one wrapped key per tenant (03 §3)."""
    with uow:
        envelope = aead.encrypt("t_1", b"raw alert payload")
        uow.commit()

    # Envelope shape: version(1)=0x01 | nonce(12) | ciphertext | tag(16) (03 §3).
    assert envelope[:1] == b"\x01", "envelope version byte must be 0x01"
    assert len(envelope) >= 1 + 12 + 16, "envelope must be version|nonce|ciphertext|tag"

    with uow:
        assert aead.decrypt("t_1", envelope) == b"raw alert payload"
        uow.commit()

    # One wrapped data key per tenant.
    assert _tenant_key_count(db, "t_1") == 1


def test_aead_tamper_detected(db: Engine, uow: SqlUnitOfWork, aead: Aead) -> None:
    """A single flipped bit in the tag/ciphertext must fail authentication (03 §3)."""
    with uow:
        envelope = aead.encrypt("t_1", b"secret")
        uow.commit()

    tampered = bytearray(envelope)
    tampered[-1] ^= 0x01  # corrupt the GCM tag
    with uow, pytest.raises(InvalidTag):
        aead.decrypt("t_1", bytes(tampered))


def test_crypto_shred_makes_payload_unreadable(db: Engine, uow: SqlUnitOfWork, aead: Aead) -> None:
    """Deleting the wrapped data key must make prior payloads permanently unreadable (03 §3)."""
    with uow:
        envelope = aead.encrypt("t_1", b"secret")
        uow.commit()

    with uow:
        aead.shred("t_1")  # delete the tenant's wrapped data key
        uow.commit()

    assert _tenant_key_count(db, "t_1") == 0, "shred must delete the wrapped data key"

    with uow, pytest.raises(InvalidTag):
        aead.decrypt("t_1", envelope)


def test_decrypt_after_shred_does_not_reprovision_key(
    db: Engine, uow: SqlUnitOfWork, aead: Aead
) -> None:
    """A read after crypto-shred fails and leaves the tenant keyless (03 §3)."""
    with uow:
        envelope = aead.encrypt("t_1", b"secret")
        aead.shred("t_1")
        uow.commit()

    with uow:
        with pytest.raises(InvalidTag):
            aead.decrypt("t_1", envelope)
        uow.commit()

    assert _tenant_key_count(db, "t_1") == 0, "decrypt must never provision a data key"


def test_wrapped_key_bound_to_tenant(db: Engine, uow: SqlUnitOfWork, aead: Aead) -> None:
    """A wrapped key copied onto another tenant's row does not unwrap (AAD = tenant)."""
    with uow:
        envelope = aead.encrypt("t_1", b"secret")
        aead.encrypt("t_2", b"other")
        uow.commit()

    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE tenant_keys SET wrapped_key = "
                "(SELECT wrapped_key FROM tenant_keys WHERE tenant_id = 't_1') "
                "WHERE tenant_id = 't_2'"
            )
        )

    with uow, pytest.raises(InvalidTag):
        aead.decrypt("t_2", envelope)


def test_blob_roundtrip(db: Engine, uow: SqlUnitOfWork) -> None:
    """Raw payloads land in ``blobs`` keyed by (tenant, key) and read back intact."""
    repo = BlobRepo(uow)
    with uow:
        repo.put(tenant_id="t_1", key="alrt_0001.raw", data=b"raw payload bytes")
        uow.commit()

    with uow:
        assert repo.get(tenant_id="t_1", key="alrt_0001.raw") == b"raw payload bytes"
        uow.commit()
