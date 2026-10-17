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
    back = snapshot.load(p, {1: KEY}, t)
    assert back.last_seq == 7 and len(back) == 2 and back.positions == 3
    assert back.substructure("CCO") == [a] and back.substructure("CCN") == []
    assert {i for i, _ in back.similar("c1ccccc1", threshold=0.99)} == {b}


def test_other_tenant_key_version_or_key_is_refused(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {1: KEY}, uuid4())
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {2: KEY}, t)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {1: os.urandom(32)}, t)


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
        snapshot.load(p, {1: KEY}, t)

    flipped = bytearray(raw)
    flipped[-1] ^= 1
    p.write_bytes(bytes(flipped))
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {1: KEY}, t)

    p.write_bytes(raw[:-50])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {1: KEY}, t)

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
        snapshot.load(p, {1: KEY}, t)


def test_not_a_snapshot_is_refused(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"hello")
    with pytest.raises(snapshot.SnapshotError):
        snapshot.load(p, {1: KEY}, uuid4())


import json
import numpy as np
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
from tests.mols import molecules


def make_two_family_index():
    smiles = molecules()[:60]
    ids = [uuid4() for _ in smiles]
    idx = engine.StructureIndex((MORGAN2, FEATMORGAN2))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    idx.mark_dead(ids[0], 2)
    idx.last_seq = 9
    return idx, ids, smiles


def test_format_two_round_trip_keeps_every_family(tmp_path):
    idx, ids, smiles = make_two_family_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=4096)
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    assert json.loads(raw[12 : 12 + hlen])["format"] == 2
    back = snapshot.load(p, {1: KEY}, t, (MORGAN2, FEATMORGAN2))
    assert [f.name for f in back.families] == ["morgan2", "featmorgan2"] and len(back) == len(ids) - 1
    for fam in ("morgan2", "featmorgan2"):
        f1, m1 = idx.fingerprints(ids, fam)
        f2, m2 = back.fingerprints(ids, fam)
        assert f1 == f2 and np.array_equal(m1, m2)
    assert {i for i, _ in back.similar(smiles[3], 0.999, family="featmorgan2")} >= {ids[3]}


def test_load_drops_unrequested_and_computes_missing_families(tmp_path):
    idx, ids, _ = make_two_family_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    fewer = snapshot.load(p, {1: KEY}, t, (MORGAN2,))
    assert [f.name for f in fewer.families] == ["morgan2"]
    more = snapshot.load(p, {1: KEY}, t, (MORGAN2, MORGAN2_CHIRAL))
    assert [f.name for f in more.families] == ["morgan2", "morgan2-chiral"]
    fresh = engine.StructureIndex((MORGAN2_CHIRAL,))
    fresh.add_batch([(i, s, 1) for i, s in zip(ids, molecules()[:60])])
    fresh.mark_dead(ids[0], 2)
    f1, m1 = more.fingerprints(ids, "morgan2-chiral")
    f2, m2 = fresh.fingerprints(ids, "morgan2-chiral")
    assert f1 == f2 and np.array_equal(m1, m2)
    assert snapshot.load(p, {1: KEY}, t).families == (MORGAN2,)      # the default


def test_a_format_one_file_raises_the_format_error_only(tmp_path, monkeypatch):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    monkeypatch.setattr(snapshot, "FORMAT_VERSION", 1)
    snapshot.save(idx, p, KEY, t, 1)
    monkeypatch.undo()
    assert snapshot.FORMAT_VERSION == 2
    with pytest.raises(snapshot.SnapshotFormatError):
        snapshot.load(p, {1: KEY}, t)
    # a header edited to claim format 2 on format-1 content is tampering, not a version mismatch
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    edited = raw[:12] + raw[12 : 12 + hlen].replace(b'"format": 1', b'"format": 2') + raw[12 + hlen :]
    assert edited != raw
    p.write_bytes(edited)
    with pytest.raises(snapshot.SnapshotError) as info:
        snapshot.load(p, {1: KEY}, t)
    assert not isinstance(info.value, snapshot.SnapshotFormatError)


def test_a_snapshot_under_a_retired_key_version_is_a_format_error(tmp_path):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    with pytest.raises(snapshot.SnapshotFormatError, match="version 1"):
        snapshot.load(p, {2: os.urandom(32)}, t)
    assert len(snapshot.load(p, {1: KEY, 2: os.urandom(32)}, t)) == len(idx)    # the key is picked by the header

