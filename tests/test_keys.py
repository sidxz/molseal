import os
import stat
from uuid import uuid4

import pytest

from molseal import crypto, keys


def test_generate_key_file_is_600_and_32_bytes(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert len(p.read_bytes()) == keys.KEY_LEN
    assert keys.FileKeyProvider(p).kek() == p.read_bytes()


def test_generate_refuses_to_overwrite(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    with pytest.raises(FileExistsError):
        keys.generate_key_file(p)


def test_group_or_world_readable_key_file_is_refused(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    os.chmod(p, 0o644)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(p).kek()


def test_wrong_length_key_file_is_refused(tmp_path):
    p = tmp_path / "kek.key"
    p.write_bytes(b"short")
    os.chmod(p, 0o600)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(p).kek()


def test_wrap_unwrap_round_trip_and_binding():
    kek = os.urandom(32)
    t = uuid4()
    secret = keys.new_tenant_secret()
    nonce, wrapped = keys.wrap_key(kek, t, "data", 1, secret)
    assert keys.unwrap_key(kek, t, "data", 1, nonce, wrapped) == secret
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, uuid4(), "data", 1, nonce, wrapped)
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, t, "hmac", 1, nonce, wrapped)
    with pytest.raises(crypto.SealError):
        keys.unwrap_key(kek, t, "data", 2, nonce, wrapped)


def test_exact_key_hmac_is_deterministic_and_keyed():
    k1, k2 = os.urandom(32), os.urandom(32)
    a = keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert len(a) == 32
    assert a == keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert a != keys.exact_key_hmac(k1, "smiles", "ABC")
    assert a != keys.exact_key_hmac(k2, "inchikey", "ABC")
    assert keys.exact_key_hmac(k1, "ab", "c") != keys.exact_key_hmac(k1, "a", "bc")
