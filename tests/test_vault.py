from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, update

from molseal import engine, keys
from molseal.payload import Payload
from molseal.store import SqlStore, StructureRecord, VersionConflict, structure_keys, structures, tenant_keys, tenants
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
    assert vault.store.tenant_state(vault.tenant_id) == (0, 0)


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
    with pytest.raises(keys.KeyProviderError):
        Vault(store, keys.FileKeyProvider(other), t)


def test_no_snapshot_dir_means_save_fails_loudly(env):
    store, kp, _ = env
    v = Vault(store, kp, uuid4())
    with pytest.raises(VaultError):
        v.save_snapshot()


def test_a_stale_read_retries_instead_of_reusing_a_row_version(env, monkeypatch):
    store, kp, _ = env
    t = uuid4()
    v1, v2 = Vault(store, kp, t), Vault(store, kp, t)
    x = uuid4()
    v1.put(x, "CCO")
    stale = store.get_structure(t, x)                # v2 reads rv 1 ...
    v1.put(x, "c1ccccc1")                            # ... v1 writes rv 2 ...
    real_get = store.get_structure
    calls = []
    monkeypatch.setattr(store, "get_structure", lambda *a: calls.append(1) or (stale if len(calls) == 1 else real_get(*a)))
    v2.put(x, "CCCl")                                # ... v2's first attempt conflicts, the retry writes rv 3
    monkeypatch.setattr(store, "get_structure", real_get)
    assert len(calls) == 2 and store.get_structure(t, x).row_version == 3
    assert v1.substructure("CCCl") == [x] and v1.substructure("c1ccccc1") == []
    assert v1.get(x).smiles == "CCCl"


def test_a_conflict_that_never_clears_raises(vault, monkeypatch):
    def always(*a):
        raise VersionConflict("x")
    monkeypatch.setattr(vault.store, "put_structure", always)
    with pytest.raises(VaultError):
        vault.put(uuid4(), "CCO")


def test_missing_key_rows_on_a_tenant_with_data_refuse_to_mint(env):
    store, kp, _ = env
    t = uuid4()
    Vault(store, kp, t).put(uuid4(), "CCO")
    with store.engine.begin() as conn:
        conn.execute(delete(tenant_keys).where(tenant_keys.c.tenant_id == t))
    before = store.tenant_state(t), store.changes_since(t, 0)
    with pytest.raises(VaultError, match=str(t)):
        Vault(store, kp, t)
    assert store.get_tenant_keys(t) == []
    assert (store.tenant_state(t), store.changes_since(t, 0)) == before


def test_an_unindexable_row_is_counted_and_does_not_loop_rebuilds(env, monkeypatch):
    store, kp, _ = env
    t = uuid4()
    v1 = Vault(store, kp, t)
    good, bad = uuid4(), uuid4()
    v1.put(good, "CCO")
    v1.put(bad, "CCN")
    # A live row whose stored SMILES RDKit cannot read (written past canonicalize, as a bad version would).
    nonce, ct = v1._seal(bad, 2, Payload("C1CC", {}, False))
    store.put_structure(StructureRecord(t, bad, 2, 1, nonce, ct, False), {}, 1)
    v2 = Vault(store, kp, t)
    assert v2.index.unindexed == {bad} and len(v2) == 1
    assert v1.substructure("CCN") == [] and v1.index.unindexed == {bad}    # old version no longer served
    rebuilds = []
    real = v2._rebuild_locked
    monkeypatch.setattr(v2, "_rebuild_locked", lambda upto: rebuilds.append(upto) or real(upto))
    v1.put(uuid4(), "CCC")
    for _ in range(3):
        v2.substructure("C")
    v2.refresh()
    assert rebuilds == []
    v1.put(bad, "CCCl")                              # a later good version leaves the set
    assert v2.substructure("CCCl") == [bad] and v2.index.unindexed == set()


def test_put_many_loads_in_chunks_with_consecutive_seqs(vault):
    from tests.mols import molecules
    smiles = molecules()[:25]
    ids = [uuid4() for _ in smiles]
    n = vault.put_many([(i, s, {"inchikey": f"K{k}"}) for k, (i, s) in enumerate(zip(ids, smiles))], chunk=10)
    assert n == 25 and len(vault) == 25
    assert vault.store.tenant_state(vault.tenant_id) == (25, 25)
    assert vault.find_exact("inchikey", "K7") == [ids[7]]
    assert set(vault.substructure("[#6]", kind="smarts")) == set(ids)
    assert vault.index.last_seq == 25


def test_put_many_edits_existing_ids_and_rejects_duplicates_and_garbage(vault):
    a, b = uuid4(), uuid4()
    vault.put_many([(a, "CCO", None), (b, "CCN", None)])
    vault.put_many([(a, "c1ccccc1", None)])
    assert vault.substructure("CCO") == [] and vault.substructure("c1ccccc1") == [a]
    assert vault.get(a).smiles == "c1ccccc1"
    with pytest.raises(ValueError):
        vault.put_many([(b, "CCO", None), (b, "CCN", None)])
    cursor_before = vault.store.tenant_state(vault.tenant_id)
    with pytest.raises(engine.StructureError):
        vault.put_many([(uuid4(), "CCO", None), (uuid4(), "garbage(", None)])
    assert vault.store.tenant_state(vault.tenant_id) == cursor_before


def test_a_second_instance_sees_a_bulk_load(env):
    store, kp, snap = env
    t = uuid4()
    v1, v2 = Vault(store, kp, t), Vault(store, kp, t)
    ids = [uuid4() for _ in range(12)]
    v1.put_many([(i, "CCO", None) for i in ids], chunk=5)
    assert set(v2.substructure("CCO")) == set(ids)


def test_put_many_retries_a_conflict_with_fresh_nonces_and_indexes_once(vault, monkeypatch):
    real_put, attempts, indexed = vault.store.put_structures, [], []

    def flaky(batch):
        attempts.append([rec.nonce for rec, _, _ in batch])
        if len(attempts) == 1:
            raise VersionConflict("simulated concurrent writer")
        return real_put(batch)

    real_add = vault.index.add_batch
    monkeypatch.setattr(vault.store, "put_structures", flaky)
    monkeypatch.setattr(vault.index, "add_batch", lambda items, n=-1: indexed.append(len(items)) or real_add(items, n))
    ids = [uuid4(), uuid4()]
    assert vault.put_many([(ids[0], "CCO", None), (ids[1], "CCN", None)]) == 2
    assert len(attempts) == 2 and not set(attempts[0]) & set(attempts[1])     # re-sealed, not resent
    assert indexed == [2] and set(vault.substructure("C")) == set(ids)


# append to tests/test_vault.py
import numpy as np
from molseal import metrics, snapshot
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL


def test_get_many_decrypts_live_rows_and_skips_deleted_and_unknown(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a, b, c = uuid4(), uuid4(), uuid4()
    v1.put_many([(a, "OCC", {"k": "1"}), (b, "CCN", None), (c, "c1ccccc1", None)])
    Vault(store, kp, t).delete(b)                                   # a second instance deletes b
    got = v1.get_many([a, b, c, uuid4()])
    assert set(got) == {a, c} and got[a].smiles == "CCO" and got[a].keys == {"k": "1"}
    assert v1.get_many([]) == {}


def test_fingerprints_sync_before_reading(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t)
    a, b = uuid4(), uuid4()
    v1.put(a, "CCO")
    Vault(store, kp, t).put(b, "CCN")                                # written elsewhere
    found, mat = v1.fingerprints([b, a])
    assert found == [b, a]
    assert np.array_equal(mat, engine.morgan_matrix([engine.Chem.MolFromSmiles("CCN"), engine.Chem.MolFromSmiles("CCO")]))
    with pytest.raises(ValueError):
        v1.fingerprints([a], "nope")


def test_families_reach_fresh_rebuilt_and_loaded_indexes(env):
    store, kp, snap = env
    t = uuid4()
    v = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2, FEATMORGAN2))
    a = uuid4()
    v.put(a, "CCOc1ccccc1")
    assert [f.name for f in v.index.families] == ["morgan2", "featmorgan2"]
    assert {i for i, _ in v.similar("CCOc1ccccc1", 0.99, family="featmorgan2", metric=metrics.dice)} == {a}
    v.rebuild()
    assert [f.name for f in v.index.families] == ["morgan2", "featmorgan2"]
    v.save_snapshot()
    same = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2, FEATMORGAN2))
    assert [f.name for f in same.index.families] == ["morgan2", "featmorgan2"]
    other = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2_CHIRAL,))
    assert [f.name for f in other.index.families] == ["morgan2-chiral"]
    assert {i for i, _ in other.similar("CCOc1ccccc1", 0.99, family="morgan2-chiral")} == {a}


def test_a_format_one_snapshot_is_rebuilt_and_resaved_as_format_two(env, monkeypatch, caplog):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a = uuid4()
    v1.put(a, "CCO")
    monkeypatch.setattr(snapshot, "FORMAT_VERSION", 1)
    path = v1.save_snapshot()
    monkeypatch.undo()
    with caplog.at_level("WARNING", logger="molseal.vault"):
        v2 = Vault(store, kp, t, snapshot_dir=snap)                  # must not raise
    warned = [r for r in caplog.records if "ignoring snapshot" in r.getMessage()]
    assert len(warned) == 1 and str(path) in warned[0].getMessage()
    assert v2.substructure("CCO") == [a]
    v2.save_snapshot()
    assert snapshot.load(path, v2.keys.data_key, t, v2.keys.data_key_version).substructure("CCO") == [a]


def test_a_tampered_snapshot_still_refuses_to_open(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    v1.put(uuid4(), "CCO")
    path = v1.save_snapshot()
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(bytes(raw))
    with pytest.raises(snapshot.SnapshotError):
        Vault(store, kp, t, snapshot_dir=snap)


def test_substructure_options_pass_through(vault):
    keto, enol, l_ala, d_ala = uuid4(), uuid4(), uuid4(), uuid4()
    vault.put_many([(keto, "O=c1cccc[nH]1", None), (enol, "Oc1ccccn1", None),
                    (l_ala, "C[C@H](N)C(=O)O", None), (d_ala, "C[C@@H](N)C(=O)O", None)])
    assert set(vault.substructure("Oc1ccccn1", generalized=True)) == {keto, enol}
    assert vault.count_substructure("Oc1ccccn1", generalized=True) == 2
    assert vault.substructure("C[C@H](N)C(=O)O", use_chirality=True) == [l_ala]
    assert set(vault.substructure("C[C@H](N)C(=O)O")) == {l_ala, d_ala}
    block = engine.Chem.MolToMolBlock(engine.Chem.MolFromSmiles("Oc1ccccn1"))
    assert vault.substructure(block, kind="molblock") == [enol]
