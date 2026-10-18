"""Tenant-scoped AEAD envelope + crypto-shred (03 §3).

Per-tenant 32-byte data keys (AES-256-GCM) wrap each tenant's payloads; the
data key itself is wrapped by the operator-held master key and stored — the
store only ever sees wrapped keys (03 §3). Crypto-shred is deleting the
wrapped data key: every previously-encrypted payload becomes permanently
unreadable (03 §3).

Envelope layout (03 §3)::

    version(1)=0x01 | nonce(12) | ciphertext | tag(16)

The ``KeyStore`` protocol below is the only seam between this package and
persistence: it is declared here and satisfied structurally by
``adapters.db.repos.blobs.SqlKeyStore`` — the two packages never import
each other (01 §2, adapters-independent).

Time is injected via the ``Clock`` port (05 §8): no wall clock in adapters.
Error taxonomy: the frozen 01 §6 taxonomy has no crypto member, so every
decrypt failure (tampered envelope, shredded key, missing key) raises
``cryptography.exceptions.InvalidTag`` — stable and documented (PR flag).
"""

from __future__ import annotations

from secrets import token_bytes
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from supportagent.domain.errors import ConfigError
from supportagent.ports.clock import Clock

ENVELOPE_VERSION = 0x01
_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32
_TAG_BYTES = 16


class KeyStore(Protocol):
    """Where wrapped tenant data keys live (03 §3).

    Satisfied structurally by ``SqlKeyStore`` (adapters.db); this package
    never imports it (01 §2: adapters stay independent).
    """

    def get_wrapped_key(self, tenant_id: str) -> bytes | None:
        """The tenant's wrapped data key, or ``None`` if none exists."""
        ...

    def put_wrapped_key(self, tenant_id: str, wrapped: bytes, created_ts: int) -> None:
        """Store or refresh the tenant's wrapped data key."""
        ...

    def delete_wrapped_key(self, tenant_id: str) -> None:
        """Delete the wrapped key (the crypto-shred primitive)."""
        ...


class Aead:
    """Encrypt/decrypt tenant payloads and crypto-shred a tenant (03 §3).

    The master key is injected as raw bytes (03 §3: the adapter never reads
    the environment itself); a 32-byte master key is required for
    AES-256-GCM. The ``Clock`` is injected (05 §8) and supplies the
    ``created_ts`` bookkeeping value for newly wrapped keys.
    """

    def __init__(self, *, master_key: bytes, keys: KeyStore, clock: Clock) -> None:
        if len(master_key) != _DATA_KEY_BYTES:
            raise ConfigError(  # noqa: TRY003
                "Aead: master key must be 32 bytes (AES-256-GCM, 03 §3)"
            )
        self._master = AESGCM(master_key)
        self._keys = keys
        self._clock = clock

    def encrypt(self, tenant_id: str, plaintext: bytes) -> bytes:
        """Return the sealed envelope ``0x01|nonce(12)|ciphertext|tag(16)`` (03 §3)."""
        data_key = self._data_key(tenant_id)
        nonce = token_bytes(_NONCE_BYTES)
        sealed = AESGCM(data_key).encrypt(nonce, plaintext, None)
        return bytes([ENVELOPE_VERSION]) + nonce + sealed

    def decrypt(self, tenant_id: str, envelope: bytes) -> bytes:
        """Open an envelope; raises ``InvalidTag`` on any failure (03 §3, PR flag).

        A wrong version byte, a truncated envelope, a missing data key, or a
        corrupted ciphertext/tag all surface as ``InvalidTag``: the frozen
        01 §6 taxonomy has no crypto member, so this is the stable,
        documented failure type (PR flag).
        """
        if len(envelope) < 1 + _NONCE_BYTES + _TAG_BYTES or envelope[0] != ENVELOPE_VERSION:
            raise InvalidTag(  # noqa: TRY003
                "unsupported envelope: wrong version or truncated payload"
            )
        data_key = self._data_key(tenant_id)
        nonce = envelope[1 : 1 + _NONCE_BYTES]
        return AESGCM(data_key).decrypt(nonce, envelope[1 + _NONCE_BYTES :], None)

    def shred(self, tenant_id: str) -> None:
        """Crypto-shred: delete the tenant's wrapped data key (03 §3).

        Every previously-encrypted payload becomes permanently unreadable;
        this cannot be undone (the data key is gone, not just the envelope).
        """
        self._keys.delete_wrapped_key(tenant_id)

    # -- internals -----------------------------------------------------------

    def _data_key(self, tenant_id: str) -> bytes:
        """The tenant's unwrapped data key, creating + wrapping it on first use."""
        wrapped = self._keys.get_wrapped_key(tenant_id)
        if wrapped is not None:
            # Wrapped-key layout: nonce(12) | wrapped_key_ciphertext+tag(16).
            return self._master.decrypt(wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:], None)
        data_key = token_bytes(_DATA_KEY_BYTES)
        nonce = token_bytes(_NONCE_BYTES)
        self._keys.put_wrapped_key(
            tenant_id,
            nonce + self._master.encrypt(nonce, data_key, None),
            int(self._clock.now().timestamp()),
        )
        return data_key
