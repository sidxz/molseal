import os
import stat
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from molseal import crypto, keys


def test_generate_key_file_is_600_and_32_bytes(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert len(p.read_bytes()) == keys.KEY_LEN


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
        keys.FileKeyProvider(p).wrap(b"label", b"x" * 32)


def test_wrong_length_or_missing_key_file_is_refused(tmp_path):
    p = tmp_path / "kek.key"
    p.write_bytes(b"short")
    os.chmod(p, 0o600)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(p).wrap(b"label", b"x" * 32)
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(tmp_path / "missing.key").unwrap(b"label", b"x" * 40)


def test_raw_provider_rejects_wrong_length_kek():
    with pytest.raises(keys.KeyProviderError):
        keys.RawKeyProvider(b"short")


def test_raw_provider_round_trip_binds_label_and_detects_tampering():
    p = keys.RawKeyProvider(os.urandom(32))
    blob = p.wrap(b"label-1", b"plain")
    assert p.unwrap(b"label-1", blob) == b"plain"
    assert p.wrap(b"label-1", b"plain") != blob                     # fresh nonce every time
    with pytest.raises(keys.KeyProviderError):
        p.unwrap(b"label-2", blob)
    with pytest.raises(keys.KeyProviderError):
        p.unwrap(b"label-1", bytes([blob[0] ^ 1]) + blob[1:])
    with pytest.raises(keys.KeyProviderError):
        keys.RawKeyProvider(os.urandom(32)).unwrap(b"label-1", blob)   # another KEK
    with pytest.raises(keys.KeyProviderError):
        p.unwrap(b"label-1", b"too short")


def test_bind_and_unbind_check_label_and_length():
    label = crypto.key_aad(uuid4(), "data", 1)
    secret = keys.new_tenant_secret()
    plain = keys._bind(label, secret)
    assert plain == struct.pack(">H", len(label)) + label + secret
    assert len(plain) == 59
    assert keys._unbind(label, plain) == secret
    with pytest.raises(keys.KeyProviderError, match="label"):
        keys._unbind(crypto.key_aad(uuid4(), "data", 1), plain)
    with pytest.raises(keys.KeyProviderError, match="short"):
        keys._unbind(label, b"\x00")
    with pytest.raises(keys.KeyProviderError, match="expected 32"):
        keys._unbind(label, keys._bind(label, b"x" * 31))


def test_wrap_tenant_key_round_trip_and_binding(tmp_path):
    p = tmp_path / "kek.key"
    keys.generate_key_file(p)
    provider = keys.FileKeyProvider(p)
    t = uuid4()
    secret = keys.new_tenant_secret()
    blob = keys.wrap_tenant_key(provider, t, "data", 1, secret)
    assert keys.unwrap_tenant_key(provider, t, "data", 1, blob) == secret
    for args in ((uuid4(), "data", 1), (t, "hmac", 1), (t, "data", 2)):
        with pytest.raises(keys.KeyProviderError):
            keys.unwrap_tenant_key(provider, *args, blob)


def test_exact_key_hmac_is_deterministic_and_keyed():
    k1, k2 = os.urandom(32), os.urandom(32)
    a = keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert len(a) == 32
    assert a == keys.exact_key_hmac(k1, "inchikey", "ABC")
    assert a != keys.exact_key_hmac(k1, "smiles", "ABC")
    assert a != keys.exact_key_hmac(k2, "inchikey", "ABC")
    assert keys.exact_key_hmac(k1, "ab", "c") != keys.exact_key_hmac(k1, "a", "bc")


def test_a_directory_as_key_path_is_a_key_file_error(tmp_path):
    with pytest.raises(keys.KeyFileError):
        keys.FileKeyProvider(tmp_path).wrap(b"l", b"x" * 59)


def test_provider_from_spec_parses_every_scheme(monkeypatch, tmp_path):
    from molseal.providers.aws import AwsKmsKeyProvider
    from molseal.providers.azure import AzureKeyVaultKeyProvider
    from molseal.providers.gcp import GcpKmsKeyProvider
    from molseal.providers.infisical import InfisicalKmsKeyProvider

    p = tmp_path / "k.key"
    bare = keys.provider_from_spec(str(p))
    assert isinstance(bare, keys.FileKeyProvider) and bare.path == Path(str(p))
    assert keys.provider_from_spec(f"file:{p}").path == Path(str(p))
    assert keys.provider_from_spec("file:C:/keys/molseal.key").path == Path("C:/keys/molseal.key")
    aws = keys.provider_from_spec("aws:alias/molseal")
    assert isinstance(aws, AwsKmsKeyProvider) and aws.key_id == "alias/molseal"
    gcp = keys.provider_from_spec("gcp:projects/p/locations/l/keyRings/r/cryptoKeys/k")
    assert isinstance(gcp, GcpKmsKeyProvider) and gcp.key_name == "projects/p/locations/l/keyRings/r/cryptoKeys/k"
    az = keys.provider_from_spec("azure:https://v.vault.azure.net/keys/molseal")
    assert isinstance(az, AzureKeyVaultKeyProvider) and az.key_id == "https://v.vault.azure.net/keys/molseal"
    monkeypatch.delenv("MOLSEAL_INFISICAL_TOKEN", raising=False)
    with pytest.raises(keys.KeyProviderError, match="MOLSEAL_INFISICAL_TOKEN"):
        keys.provider_from_spec("infisical:https://eu.infisical.com/4f1c")
    monkeypatch.setenv("MOLSEAL_INFISICAL_TOKEN", "tok")
    inf = keys.provider_from_spec("infisical:https://eu.infisical.com/4f1c")
    assert isinstance(inf, InfisicalKmsKeyProvider)
    assert (inf.base_url, inf.key_id, inf.token) == ("https://eu.infisical.com", "4f1c", "tok")
    for bad in ("unknown:x", "aws:", "gcp:", "azure:", "infisical:", "infisical:nokey",
                "infisical:http://kms.example/k"):
        with pytest.raises(keys.KeyProviderError):
            keys.provider_from_spec(bad)
