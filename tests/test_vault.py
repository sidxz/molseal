from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, update

from molseal import crypto, engine, keys
from molseal.store import SqlStore, structure_keys, structures, tenants
from molseal.vault import NotFound, Vault, VaultError


@pytest.fixture
def env(tmp_path):
    sa_engine = sa.create_engine(f"sqlite:///{tmp_path / 'v.db'}")
    store = SqlStore(sa_engine)
    store.create_tables()
    kp = tmp_path / "kek.key"
    keys.generate_key_file(kp)
    return store, keys.FileKeyProvider(kp), tmp_path / "snap"


@pytest.fixture
def vault(env):
    store, kp, snap = env
    return Vault(store, kp, uuid4(), snapshot_dir=snap)


def test_put_get_round_trip_stores_canonical_smiles(vault):
    i = uuid4()
    seq = vault.put(i, "OCC", {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"})
    assert seq == 1
    p = vault.get(i)
    assert p.smiles == "CCO" and p.keys == {"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"} and not p.deleted
    assert vault.get(uuid4()) is None
    assert len(vault) == 1


def test_unparseable_smiles_writes_nothing(vault):
    with pytest.raises(engine.StructureError):
        vault.put(uuid4(), "garbage(")
    assert vault.store.seq_cursor(vault.tenant_id) == 0
    assert vault.store.live_count(vault.tenant_id) == 0


def test_read_your_writes_for_put_edit_and_delete(vault):
    x = uuid4()
    vault.put(x, "CCO")
    assert vault.substructure("CCO") == [x]
    vault.put(x, "c1ccccc1")
    assert vault.substructure("CCO") == [] and vault.substructure("c1ccccc1") == [x]
    assert vault.get(x).smiles == "c1ccccc1"
    vault.delete(x)
    assert vault.substructure("c1ccccc1") == [] and vault.get(x) is None and len(vault) == 0
    with pytest.raises(NotFound):
        vault.delete(uuid4())


def test_find_exact_verifies_against_the_payload(vault):
    a, b = uuid4(), uuid4()
    vault.put(a, "CCO", {"inchikey": "AAA"})
    vault.put(b, "CCN", {"inchikey": "BBB"})
    assert vault.find_exact("inchikey", "AAA") == [a]
    assert vault.find_exact("inchikey", "nope") == []
    assert vault.find_exact("other", "AAA") == []
    # A writer copies A's hmac row onto B. The index says B; the payload says no.
    h = keys.exact_key_hmac(vault.keys.hmac_key, "inchikey", "AAA")
    with vault.store.engine.begin() as conn:
        conn.execute(update(structure_keys).where(structure_keys.c.id == b).values(hmac=h))
    assert vault.find_exact("inchikey", "AAA") == [a]
    vault.delete(a)
    assert vault.find_exact("inchikey", "AAA") == []


def test_a_second_instance_sees_writes_through_the_cursor(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t)
    v2 = Vault(store, kp, t)
    assert v1.keys == v2.keys
    x = uuid4()
    v1.put(x, "CCO")
    assert v2.substructure("CCO") == [x]
    assert {i for i, _ in v2.similar("CCO", threshold=0.99)} == {x}
    v1.delete(x)
    assert v2.count_substructure("CCO") == 0


def test_rollback_in_the_store_triggers_a_rebuild(vault):
    ids = [uuid4() for _ in range(3)]
    for i in ids:
        vault.put(i, "CCO")
    assert len(vault) == 3
    with vault.store.engine.begin() as conn:
        conn.execute(delete(structures).where(structures.c.id == ids[2]))
        conn.execute(delete(structure_keys).where(structure_keys.c.id == ids[2]))
        conn.execute(update(tenants).where(tenants.c.tenant_id == vault.tenant_id).values(seq_cursor=2))
    assert set(vault.substructure("CCO")) == set(ids[:2])
    assert len(vault) == 2


def test_snapshot_save_load_and_catch_up(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a, b = uuid4(), uuid4()
    v1.put(a, "CCO")
    path = v1.save_snapshot()
    assert path.exists()
    v1.put(b, "CCN")
    v3 = Vault(store, kp, t, snapshot_dir=snap)
    assert v3.index.positions >= 2
    assert set(v3.substructure("[CX4]", kind="smarts")) == {a, b}


def test_wrong_kek_cannot_open_an_existing_tenant(env, tmp_path):
    store, kp, snap = env
    t = uuid4()
    Vault(store, kp, t).put(uuid4(), "CCO")
    other = tmp_path / "other.key"
    keys.generate_key_file(other)
    with pytest.raises(crypto.SealError):
        Vault(store, keys.FileKeyProvider(other), t)


def test_no_snapshot_dir_means_save_fails_loudly(env):
    store, kp, _ = env
    v = Vault(store, kp, uuid4())
    with pytest.raises(VaultError):
        v.save_snapshot()
