"""Key hierarchy: a key-encryption key (KEK) from a provider wraps two secrets per tenant."""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .crypto import key_aad, open_sealed, seal

KEY_LEN = 32


class KeyProvider(Protocol):
    """Where the KEK comes from. Implementations: a 0600 file (here), a KMS (consumer's job)."""

    def kek(self) -> bytes: ...


class KeyFileError(Exception):
    pass


def generate_key_file(path: Path) -> None:
    """Write 32 random bytes to a new file with mode 0600. Refuses to overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(KEY_LEN))


@dataclass(frozen=True)
class FileKeyProvider:
    path: Path

    def kek(self) -> bytes:
        p = Path(self.path)
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            raise KeyFileError(f"{p} is readable by group or others (mode {mode:o}); chmod 600 it")
        data = p.read_bytes()
        if len(data) != KEY_LEN:
            raise KeyFileError(f"{p} must hold exactly {KEY_LEN} bytes, has {len(data)}")
        return data


@dataclass(frozen=True)
class TenantKeys:
    data_key: bytes
    data_key_version: int
    hmac_key: bytes
    hmac_key_version: int


def new_tenant_secret() -> bytes:
    return os.urandom(KEY_LEN)


def wrap_key(kek: bytes, tenant_id: UUID, kind: str, version: int, secret: bytes) -> tuple[bytes, bytes]:
    """Returns (nonce, wrapped). The label pins the secret to one tenant, kind and version."""
    return seal(kek, secret, key_aad(tenant_id, kind, version))


def unwrap_key(kek: bytes, tenant_id: UUID, kind: str, version: int, nonce: bytes, wrapped: bytes) -> bytes:
    return open_sealed(kek, nonce, wrapped, key_aad(tenant_id, kind, version))


def exact_key_hmac(hmac_key: bytes, kind: str, value: str) -> bytes:
    """Keyed hash of a caller-defined exact key. Same (key, kind, value) gives the same bytes."""
    msg = kind.encode("utf-8") + b"\x00" + value.encode("utf-8")
    return hmac.new(hmac_key, msg, hashlib.sha256).digest()
