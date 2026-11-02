"""The credential vault (ADR-0019, WP-O5b): bound credentials, in one encrypted file.

The secret store ``ports.credentials`` names, never the database: the database
holds a request's status and its probes' results, this file holds the secret.

- Sealed with AES-256-GCM under a key derived from the operator's master key
  (HKDF-SHA256, its own ``info``), so it needs no database to open and a copy
  of the database alone can't.
- The tenant id is the additional data: one tenant's vault doesn't open as
  another's.
- Written whole, to a temporary file that is ``0600`` before any byte lands,
  then renamed over the old one: a crash leaves the old vault or the new one.

Layout: ``version(1)=0x01 | nonce(12) | ciphertext | tag(16)``, the plaintext
a JSON object of credentials by request id.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from supportagent.domain.errors import ConfigError
from supportagent.ports.credentials import StoredCredential

_VERSION = 0x01
_NONCE = 12
_KEY = 32
_INFO = b"supportagent/credential-vault/v1"
_MODE = 0o600


class FileCredentialVault:
    def __init__(self, path: Path, *, master_key: bytes, tenant_id: str) -> None:
        if len(master_key) != _KEY:
            raise ConfigError("credential vault: the master key must be 32 bytes")  # noqa: TRY003
        key = HKDF(algorithm=hashes.SHA256(), length=_KEY, salt=None, info=_INFO).derive(master_key)
        self._aead = AESGCM(key)
        self._path = path
        self._aad = tenant_id.encode()

    @property
    def path(self) -> Path:
        return self._path

    def put(self, credential: StoredCredential) -> None:
        entries = self._load()
        entries[credential.request] = {
            "plugin": credential.plugin,
            "access": credential.access,
            "target": credential.target,
            "backend": credential.backend,
            "bound_ts": credential.bound_ts,
            "secret": credential.secret,
        }
        self._save(entries)

    def get(self, request: str) -> StoredCredential | None:
        entry = self._load().get(request)
        return None if entry is None else _credential(request, entry)

    def remove(self, request: str) -> bool:
        entries = self._load()
        if entries.pop(request, None) is None:
            return False
        self._save(entries)
        return True

    def all(self) -> list[StoredCredential]:
        return [_credential(rid, entry) for rid, entry in sorted(self._load().items())]

    def _load(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        sealed = self._path.read_bytes()
        if len(sealed) < 1 + _NONCE or sealed[0] != _VERSION:
            raise ConfigError(f"credential vault {self._path}: not a vault")  # noqa: TRY003
        try:
            plain = self._aead.decrypt(sealed[1 : 1 + _NONCE], sealed[1 + _NONCE :], self._aad)
        except InvalidTag as exc:
            raise ConfigError(  # noqa: TRY003
                f"credential vault {self._path}: doesn't open with this master key and tenant"
            ) from exc
        loaded = json.loads(plain)
        return {str(k): dict(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}

    def _save(self, entries: dict[str, dict[str, object]]) -> None:
        nonce = token_bytes(_NONCE)
        plain = json.dumps(entries, sort_keys=True).encode()
        sealed = bytes([_VERSION]) + nonce + self._aead.encrypt(nonce, plain, self._aad)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _MODE)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(sealed)
                out.flush()
                os.fsync(out.fileno())
            tmp.replace(self._path)
        finally:
            tmp.unlink(missing_ok=True)


def _credential(request: str, entry: dict[str, object]) -> StoredCredential:
    return StoredCredential(
        request=request,
        plugin=str(entry["plugin"]),
        access=str(entry["access"]),
        target=str(entry["target"]),
        backend=str(entry["backend"]),
        bound_ts=int(str(entry["bound_ts"])),
        secret=str(entry["secret"]),
    )


__all__ = ["FileCredentialVault"]
