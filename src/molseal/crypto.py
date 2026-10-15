"""Byte-level primitives: AES-256-GCM with a label (AAD), size-class padding, chunked sealing.

Nothing here knows about chemistry, databases or files.
"""

from __future__ import annotations

import os
import struct
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import MolsealError

NONCE_LEN = 12
TAG_LEN = 16
MIN_SIZE_CLASS = 256
CHUNK_SIZE = 64 * 1024 * 1024


class SealError(MolsealError):
    """Ciphertext did not open: wrong key, wrong label, or tampered bytes."""


def size_class(n: int, min_class: int = MIN_SIZE_CLASS) -> int:
    """Smallest power-of-two class >= n, never below min_class."""
    c = min_class
    while c < n:
        c *= 2
    return c


def pad(data: bytes, min_class: int = MIN_SIZE_CLASS) -> bytes:
    """data + zero fill + 4-byte big-endian true length, sized to a power-of-two class."""
    total = size_class(len(data) + 4, min_class)
    return data + b"\x00" * (total - len(data) - 4) + struct.pack(">I", len(data))


def unpad(padded: bytes) -> bytes:
    if len(padded) < 4:
        raise SealError("padded data too short")
    (n,) = struct.unpack(">I", padded[-4:])
    if n > len(padded) - 4:
        raise SealError("bad padding length")
    return padded[:n]


def seal(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """Encrypt with a fresh random nonce. Returns (nonce, ciphertext)."""
    nonce = os.urandom(NONCE_LEN)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """Decrypt and authenticate; the label must equal the one given to seal."""
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SealError("ciphertext did not open") from exc


def row_aad(tenant_id: UUID, id: UUID, key_version: int, row_version: int) -> bytes:
    """The label of a structure row: it binds the ciphertext to its tenant, id and versions."""
    return tenant_id.bytes + id.bytes + struct.pack(">II", key_version, row_version)


def key_aad(tenant_id: UUID, kind: str, version: int) -> bytes:
    """The label of a wrapped tenant key: tenant, kind ('data' or 'hmac') and version."""
    return tenant_id.bytes + kind.encode("ascii") + b"\x00" + struct.pack(">I", version)


def chunk_aad(prefix: bytes, index: int, count: int) -> bytes:
    return prefix + struct.pack(">II", index, count)


def seal_chunks(
    key: bytes, data: bytes, aad_prefix: bytes, chunk_size: int = CHUNK_SIZE
) -> list[tuple[bytes, bytes]]:
    """Split data into chunks and seal each with a label naming its index and the chunk count."""
    count = max(1, -(-len(data) // chunk_size))
    return [
        seal(key, data[i * chunk_size : (i + 1) * chunk_size], chunk_aad(aad_prefix, i, count))
        for i in range(count)
    ]


def open_chunks(key: bytes, chunks: list[tuple[bytes, bytes]], aad_prefix: bytes) -> bytes:
    """Inverse of seal_chunks; a reordered, dropped or duplicated chunk fails its label check."""
    count = len(chunks)
    return b"".join(
        open_sealed(key, nonce, ct, chunk_aad(aad_prefix, i, count))
        for i, (nonce, ct) in enumerate(chunks)
    )
