import os
from uuid import uuid4

import pytest

from molseal import crypto

KEY = bytes(range(32))


@pytest.mark.parametrize("n,expected", [(0, 256), (1, 256), (252, 256), (253, 512), (508, 512), (509, 1024)])
def test_pad_lands_on_the_smallest_power_of_two_class(n, expected):
    data = os.urandom(n)
    padded = crypto.pad(data)
    assert len(padded) == expected
    assert crypto.unpad(padded) == data


def test_unpad_rejects_garbage():
    with pytest.raises(crypto.SealError):
        crypto.unpad(b"\x00\x00\x00\xff")


def test_seal_open_round_trip_with_aad():
    nonce, ct = crypto.seal(KEY, b"hello", b"label")
    assert len(nonce) == crypto.NONCE_LEN
    assert crypto.open_sealed(KEY, nonce, ct, b"label") == b"hello"


def test_wrong_aad_wrong_key_and_tamper_all_fail():
    nonce, ct = crypto.seal(KEY, b"hello", b"label")
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(KEY, nonce, ct, b"other")
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(bytes(32), nonce, ct, b"label")
    bad = bytes([ct[0] ^ 1]) + ct[1:]
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(KEY, nonce, bad, b"label")


def test_every_seal_uses_a_fresh_nonce():
    a = crypto.seal(KEY, b"same", b"same")
    b = crypto.seal(KEY, b"same", b"same")
    assert a[0] != b[0]
    assert a[1] != b[1]


def test_row_and_key_aad_are_fixed_width_and_distinct():
    t, i = uuid4(), uuid4()
    assert len(crypto.row_aad(t, i, 1, 1)) == 16 + 16 + 8
    assert crypto.row_aad(t, i, 1, 1) != crypto.row_aad(t, i, 1, 2)
    assert crypto.row_aad(t, i, 1, 1) != crypto.row_aad(t, i, 2, 1)
    assert crypto.key_aad(t, "data", 1) != crypto.key_aad(t, "hmac", 1)


def test_chunks_round_trip_and_detect_reorder_drop_tamper():
    data = os.urandom(1000)
    chunks = crypto.seal_chunks(KEY, data, b"pre", chunk_size=400)
    assert len(chunks) == 3
    assert crypto.open_chunks(KEY, chunks, b"pre") == data
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, [chunks[1], chunks[0], chunks[2]], b"pre")
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks[:2], b"pre")
    n, c = chunks[2]
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks[:2] + [(n, bytes([c[0] ^ 1]) + c[1:])], b"pre")
    with pytest.raises(crypto.SealError):
        crypto.open_chunks(KEY, chunks, b"other")


def test_empty_data_is_one_chunk():
    chunks = crypto.seal_chunks(KEY, b"", b"pre")
    assert len(chunks) == 1
    assert crypto.open_chunks(KEY, chunks, b"pre") == b""
