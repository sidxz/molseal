"""Encrypted, chunked on-disk snapshot of a StructureIndex."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from collections.abc import Sequence
from uuid import UUID

from .crypto import CHUNK_SIZE, NONCE_LEN, SealError, open_chunks, seal_chunks
from .engine import DEFAULT_FAMILIES, FingerprintSpec, StructureIndex

FORMAT_VERSION = 2
MAGIC = b"MOLSEAL1"


class SnapshotError(Exception):
    pass


class SnapshotFormatError(SnapshotError):
    """A snapshot in a format this build does not read. Safe to ignore and rebuild, but the header is not
    authenticated, so an edited format number raises this too."""


def aad_prefix(tenant_id: UUID, key_version: int, last_seq: int) -> bytes:
    return MAGIC + tenant_id.bytes + struct.pack(">IIQ", FORMAT_VERSION, key_version, last_seq)


def save(index: StructureIndex, path: Path, data_key: bytes, tenant_id: UUID, key_version: int,
         chunk_size: int = CHUNK_SIZE) -> None:
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


def load(path: Path, data_key: bytes, tenant_id: UUID, key_version: int,
         families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> StructureIndex:
    # ponytail: whole file in memory; stream chunk by chunk when snapshots pass a few GB
    data = Path(path).read_bytes()
    if data[:8] != MAGIC or len(data) < 12:
        raise SnapshotError("not a molseal snapshot")
    (hlen,) = struct.unpack(">I", data[8:12])
    try:
        header = json.loads(data[12 : 12 + hlen])
    except ValueError as exc:
        raise SnapshotError("bad snapshot header") from exc
    if header.get("format") != FORMAT_VERSION:
        raise SnapshotFormatError(f"snapshot format {header.get('format')!r}; this build reads {FORMAT_VERSION}")
    if header.get("tenant_id") != str(tenant_id) or header.get("key_version") != key_version:
        raise SnapshotError("snapshot belongs to another tenant or key version")
    off = 12 + hlen
    chunks = []
    for _ in range(int(header["chunk_count"])):
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
    try:
        blob = open_chunks(data_key, chunks, aad_prefix(tenant_id, key_version, int(header["last_seq"])))
    except SealError as exc:
        raise SnapshotError("snapshot did not open: wrong key, edited header, or tampered chunk") from exc
    parts: dict[str, bytes] = {}
    off = 0

    def take() -> bytes:
        nonlocal off
        (plen,) = struct.unpack(">Q", blob[off : off + 8])
        off += 8
        part = blob[off : off + plen]
        off += plen
        return part

    parts["meta"] = take()
    for k in json.loads(parts["meta"])["parts"]:
        parts[k] = take()
    index = StructureIndex.from_parts(parts, families)
    index.last_seq = int(header["last_seq"])
    return index
