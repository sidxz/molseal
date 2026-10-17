"""Encrypted, chunked on-disk snapshot of a StructureIndex.

File layout: MAGIC, a 4-byte header length, a JSON header (format, tenant, key version, last_seq,
chunk count), then per chunk an 8-byte ciphertext length, the nonce and the ciphertext. The header is
plaintext, but its fields are bound into every chunk's label, so an edited header fails to open."""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from .crypto import CHUNK_SIZE, NONCE_LEN, SealError, open_chunks, seal_chunks
from .engine import DEFAULT_FAMILIES, FingerprintSpec, StructureIndex
from .errors import MolsealError

FORMAT_VERSION = 2
MAGIC = b"MOLSEAL1"       # file signature only; the readable format is FORMAT_VERSION in the header


class SnapshotError(MolsealError):
    """The file is not a snapshot of this tenant, is damaged, or did not open."""


class SnapshotFormatError(SnapshotError):
    """A snapshot the vault may discard and rebuild: a format this build does not read, or a data key version the
    tenant no longer has (retired by a key rotation). The header is not authenticated, so an edited format or
    version raises this too."""


def aad_prefix(tenant_id: UUID, key_version: int, last_seq: int) -> bytes:
    """The label every chunk is sealed under; the chunk index and count are appended per chunk."""
    return MAGIC + tenant_id.bytes + struct.pack(">IIQ", FORMAT_VERSION, key_version, last_seq)


def save(index: StructureIndex, path: Path, data_key: bytes, tenant_id: UUID, key_version: int,
         chunk_size: int = CHUNK_SIZE) -> None:
    """Write `index` to `path` encrypted under `data_key`, replacing any existing file atomically."""
    parts = index.to_parts()
    order = ["meta"] + json.loads(parts["meta"])["parts"]
    # ponytail: the whole blob is built and sealed in memory (parts plus ciphertext, roughly twice the
    # snapshot size); stream chunk by chunk when snapshots pass a few GB
    blob = b"".join(struct.pack(">Q", len(parts[k])) + parts[k] for k in order)
    chunks = seal_chunks(data_key, blob, aad_prefix(tenant_id, key_version, index.last_seq), chunk_size)
    header = json.dumps({"format": FORMAT_VERSION, "tenant_id": str(tenant_id), "key_version": key_version,
                         "last_seq": index.last_seq, "chunk_count": len(chunks)}, sort_keys=True).encode("ascii")
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(header)))
        f.write(header)
        for nonce, ct in chunks:
            f.write(struct.pack(">Q", len(ct)))
            f.write(nonce)
            f.write(ct)
    os.replace(tmp, path)


def _header(data: bytes) -> tuple[dict[str, Any], int]:
    """The JSON header and the offset of the first chunk."""
    if data[:8] != MAGIC or len(data) < 12:
        raise SnapshotError("not a molseal snapshot")
    (hlen,) = struct.unpack(">I", data[8:12])
    try:
        header: dict[str, Any] = json.loads(data[12 : 12 + hlen])
    except ValueError as exc:
        raise SnapshotError("bad snapshot header") from exc
    return header, 12 + hlen


def _chunks(data: bytes, off: int, count: int) -> list[tuple[bytes, bytes]]:
    """(nonce, ciphertext) frames from `off` to the end of the file, each length checked."""
    chunks = []
    for _ in range(count):
        if off + 8 + NONCE_LEN > len(data):
            raise SnapshotError("snapshot is truncated")
        (clen,) = struct.unpack(">Q", data[off : off + 8])
        off += 8
        nonce, ct = data[off : off + NONCE_LEN], data[off + NONCE_LEN : off + NONCE_LEN + clen]
        off += NONCE_LEN + clen
        if len(ct) != clen:
            raise SnapshotError("snapshot is truncated")
        chunks.append((nonce, ct))
    if off != len(data):
        raise SnapshotError("snapshot has trailing bytes")
    return chunks


def _split_parts(blob: bytes) -> dict[str, bytes]:
    """Length-prefixed parts of the decrypted blob: `meta` first, then the parts it lists, in order."""
    off = 0

    def take() -> bytes:
        nonlocal off
        (plen,) = struct.unpack(">Q", blob[off : off + 8])
        off += 8
        part = blob[off : off + plen]
        off += plen
        return part

    parts = {"meta": take()}
    for k in json.loads(parts["meta"])["parts"]:
        parts[k] = take()
    return parts


def load(path: Path, data_keys: Mapping[int, bytes], tenant_id: UUID,
         families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> StructureIndex:
    """Read a snapshot written by `save` for the same tenant, under whichever data key version in `data_keys`
    (version -> key) its header names; a vault passes every version it holds, so a snapshot from before a key
    rotation opens until the rotation finishes. `families` decides which fingerprint matrices come back; one
    the file lacks is recomputed from the stored structures."""
    # ponytail: whole file in memory; stream chunk by chunk when snapshots pass a few GB
    data = Path(path).read_bytes()
    header, off = _header(data)
    if header.get("format") != FORMAT_VERSION:
        raise SnapshotFormatError(f"snapshot format {header.get('format')!r}; this build reads {FORMAT_VERSION}")
    if header.get("tenant_id") != str(tenant_id):
        raise SnapshotError("snapshot belongs to another tenant")
    version = header.get("key_version")
    if not isinstance(version, int) or version not in data_keys:
        raise SnapshotFormatError(f"snapshot is under data key version {version!r}, which the tenant no longer has")
    chunks = _chunks(data, off, int(header["chunk_count"]))
    try:
        blob = open_chunks(data_keys[version], chunks, aad_prefix(tenant_id, version, int(header["last_seq"])))
    except SealError as exc:
        raise SnapshotError("snapshot did not open: wrong key, edited header, or tampered chunk") from exc
    index = StructureIndex.from_parts(_split_parts(blob), families)
    index.last_seq = int(header["last_seq"])
    return index
