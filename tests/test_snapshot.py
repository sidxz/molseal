import os
import struct
from uuid import uuid4

import pytest

from molseal import engine, snapshot

KEY = os.urandom(32)


def make_index():
    idx = engine.StructureIndex()
    a, b, c = uuid4(), uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "c1ccccc1", 1), (c, "CCN", 1)])
    idx.mark_dead(c, 2)
    idx.last_seq = 7
    return idx, a, b


def test_round_trip_with_several_chunks(tmp_path):
    idx, a, b = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=500)
    assert p.read_bytes()[:8] == snapshot.MAGIC
    back = snapshot.load(p, KEY, t, 1)
    assert back.last_seq == 7 and len(back) == 2 and back.positions == 3
    assert back.substructure("CCO") == [a] and back.substructure("CCN") == []
    assert {i for i, _ in back.similar("c1ccccc1", threshold=0.99)} == {b}


def test_other_tenant_key_version_or_key_is_refused(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, uuid4(), 1)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 2)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, os.urandom(32), t, 1)


def test_header_edit_chunk_tamper_truncation_and_reorder_are_refused(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=500)
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    header = raw[12 : 12 + hlen]
    body = raw[12 + hlen :]

    edited = raw[:12] + header.replace(b'"last_seq": 7', b'"last_seq": 8') + body
    assert edited != raw
    p.write_bytes(edited)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    flipped = bytearray(raw)
    flipped[-1] ^= 1
    p.write_bytes(bytes(flipped))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    p.write_bytes(raw[:-50])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)

    # swap the first two chunks
    off = 12 + hlen
    chunks = []
    while off < len(raw):
        (clen,) = struct.unpack(">Q", raw[off : off + 8])
        chunks.append(raw[off : off + 8 + 12 + clen])
        off += 8 + 12 + clen
    assert len(chunks) >= 2
    p.write_bytes(raw[: 12 + hlen] + chunks[1] + chunks[0] + b"".join(chunks[2:]))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, t, 1)


def test_not_a_snapshot_is_refused(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"hello")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, KEY, uuid4(), 1)
