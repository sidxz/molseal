"""The surface a library user sees: top-level names, one exception base, no secrets in reprs, no
import-time side effects, and the parameter names and return values the README documents."""

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

import molseal
from molseal import keys, payload

EXPORTED = [
    "Vault", "SqlStore", "StructureStore", "FileKeyProvider", "RawKeyProvider", "KeyProvider",
    "provider_from_spec", "generate_key_file", "rewrap_tenant", "FingerprintSpec", "MORGAN2",
    "MORGAN2_CHIRAL", "FEATMORGAN2", "Payload", "canonicalize", "metrics", "MolsealError",
    "StructureError", "KeyProviderError", "KeyFileError", "VaultError", "NotFound", "VersionConflict",
    "SealError", "PayloadError", "SnapshotError", "SnapshotFormatError", "rotate_tenant_keys", "KeyVersionChanged",
]
ERRORS = [n for n in EXPORTED if n.endswith(("Error", "Conflict", "Changed")) or n == "NotFound"]


def test_top_level_exports_cover_the_documented_surface():
    assert [n for n in EXPORTED if not hasattr(molseal, n)] == []
    assert set(EXPORTED) <= set(molseal.__all__)


def test_every_error_shares_one_base():
    for name in ERRORS:
        if name != "MolsealError":
            assert issubclass(getattr(molseal, name), molseal.MolsealError), name
    assert issubclass(molseal.StructureError, ValueError)


def test_tenant_keys_repr_hides_the_secrets():
    r = repr(keys.TenantKeys({1: b"\x01" * 32}, {1: b"\x02" * 32}))
    assert "\\x01" not in r and "\\x02" not in r and r == "TenantKeys(data=[1], hmac=[1])"


def test_package_ships_py_typed():
    assert (Path(molseal.__file__).parent / "py.typed").exists()


def test_importing_the_library_leaves_rdkit_logging_alone():
    code = "import molseal, molseal.engine; from rdkit import rdBase; print(rdBase.LogStatus())"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "rdApp.error:enabled" in out and "rdApp.warning:enabled" in out


@pytest.mark.parametrize("bad", [b'{"v":1,"smiles":1,"keys":{},"deleted":false}',
                                 b'{"v":1,"smiles":"C","keys":{"a":1},"deleted":false}'])
def test_payload_decode_rejects_wrong_field_types(bad):
    with pytest.raises(payload.PayloadError):
        payload.decode(bad)


def test_fingerprint_spec_validates_field_types():
    with pytest.raises(ValueError):
        molseal.FingerprintSpec(123, 2048, lambda: None)


@pytest.fixture
def vault(tmp_path):
    store = molseal.SqlStore(sa.create_engine(f"sqlite:///{tmp_path / 'v.db'}"))
    store.create_tables()
    kp = tmp_path / "kek.key"
    molseal.generate_key_file(kp)
    return molseal.Vault(store, molseal.FileKeyProvider(kp), uuid4())


def test_put_and_delete_return_the_row_version(vault):
    a, b = uuid4(), uuid4()
    vault.put(b, "C")                       # so the change sequence and the row version differ
    assert vault.put(a, "CCO") == 1
    assert vault.put(a, "CCN") == 2
    assert vault.delete(a) == 3


def test_search_and_put_use_the_documented_parameter_names(vault):
    a = uuid4()
    vault.put(a, "CCO", keys={"inchikey": "K"})
    assert vault.find_exact("inchikey", "K") == [a]
    assert vault.similar(query="CCO", threshold=0.99) == [(a, 1.0)]
    assert vault.substructure(query="CO") == [a]


def test_vault_keeps_secrets_and_index_private(vault):
    assert not hasattr(vault, "keys") and not hasattr(vault, "index")
