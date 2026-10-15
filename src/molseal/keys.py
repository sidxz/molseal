"""Key hierarchy: a provider wraps two secrets per tenant under a key-encryption key (KEK).

The library never needs the KEK itself. `RawKeyProvider` holds one in process (a file, or bytes the
application fetched from a secrets manager); the providers under `molseal.providers` ask a key
management service to wrap and unwrap instead. Either way the label (tenant, kind, version) is bound
into the wrapped plaintext here and checked on unwrap, so a wrapped key opens only where it was made,
whatever the provider does with the label.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from .crypto import NONCE_LEN, SealError, key_aad, open_sealed, seal

KEY_LEN = 32


class KeyProviderError(Exception):
    """The provider could not wrap or unwrap: wrong key, altered blob, label mismatch, or service failure."""


class KeyFileError(KeyProviderError):
    """Key file is missing, has the wrong length, or is readable by others."""


class KeyProvider(Protocol):
    """Wraps and unwraps bytes under a key-encryption key the library never has to see.

    `label` is the tenant/kind/version encoding; providers with associated data or an encryption
    context pass it through, others ignore it. `blob` is opaque to the library."""

    def wrap(self, label: bytes, plaintext: bytes) -> bytes: ...
    def unwrap(self, label: bytes, blob: bytes) -> bytes: ...


def generate_key_file(path: Path) -> None:
    """Write 32 random bytes to a new file with mode 0600. Refuses to overwrite."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(os.urandom(KEY_LEN))


@dataclass(frozen=True)
class RawKeyProvider:
    """AES-256-GCM in process, label as associated data. For a KEK held in config or fetched from a
    secrets manager by the application's own client."""

    kek: bytes

    def __post_init__(self) -> None:
        if len(self.kek) != KEY_LEN:
            raise KeyProviderError(f"KEK must be {KEY_LEN} bytes, got {len(self.kek)}")

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        nonce, ct = seal(self.kek, plaintext, label)
        return nonce + ct

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        if len(blob) < NONCE_LEN + 16:
            raise KeyProviderError("wrapped key blob is too short")
        try:
            return open_sealed(self.kek, blob[:NONCE_LEN], blob[NONCE_LEN:], label)
        except SealError as exc:
            raise KeyProviderError("wrapped key did not open: wrong KEK, wrong label, or altered blob") from exc


@dataclass(frozen=True)
class FileKeyProvider:
    """A 32-byte KEK in a file with mode 0600. Read on every call: a vault opens with at most four."""

    path: Path

    def _read(self) -> bytes:
        p = Path(self.path)
        try:
            mode = stat.S_IMODE(p.stat().st_mode)
        except FileNotFoundError as exc:
            raise KeyFileError(f"{p} does not exist") from exc
        if mode & 0o077:
            raise KeyFileError(f"{p} is readable by group or others (mode {mode:o}); chmod 600 it")
        data = p.read_bytes()
        if len(data) != KEY_LEN:
            raise KeyFileError(f"{p} must hold exactly {KEY_LEN} bytes, has {len(data)}")
        return data

    def wrap(self, label: bytes, plaintext: bytes) -> bytes:
        return RawKeyProvider(self._read()).wrap(label, plaintext)

    def unwrap(self, label: bytes, blob: bytes) -> bytes:
        return RawKeyProvider(self._read()).unwrap(label, blob)


@dataclass(frozen=True)
class TenantKeys:
    data_key: bytes
    data_key_version: int
    hmac_key: bytes
    hmac_key_version: int


def new_tenant_secret() -> bytes:
    return os.urandom(KEY_LEN)


def _bind(label: bytes, secret: bytes) -> bytes:
    return struct.pack(">H", len(label)) + label + secret


def _unbind(label: bytes, plaintext: bytes) -> bytes:
    if len(plaintext) < 2:
        raise KeyProviderError("unwrapped plaintext too short")
    (n,) = struct.unpack(">H", plaintext[:2])
    if n != len(label) or not hmac.compare_digest(plaintext[2:2 + n], label):
        raise KeyProviderError("wrapped key label does not match tenant, kind and version")
    secret = plaintext[2 + n:]
    if len(secret) != KEY_LEN:
        raise KeyProviderError(f"unwrapped secret is {len(secret)} bytes, expected {KEY_LEN}")
    return secret


def wrap_tenant_key(provider: KeyProvider, tenant_id: UUID, kind: str, version: int, secret: bytes) -> bytes:
    """Bind the label into the plaintext, then let the provider wrap it."""
    label = key_aad(tenant_id, kind, version)
    return provider.wrap(label, _bind(label, secret))


def unwrap_tenant_key(provider: KeyProvider, tenant_id: UUID, kind: str, version: int, blob: bytes) -> bytes:
    """Let the provider unwrap, then check the label it was made under. Raises KeyProviderError."""
    label = key_aad(tenant_id, kind, version)
    return _unbind(label, provider.unwrap(label, blob))


def exact_key_hmac(hmac_key: bytes, kind: str, value: str) -> bytes:
    """Keyed hash of a caller-defined exact key. Same (key, kind, value) gives the same bytes."""
    msg = kind.encode("utf-8") + b"\x00" + value.encode("utf-8")
    return hmac.new(hmac_key, msg, hashlib.sha256).digest()
