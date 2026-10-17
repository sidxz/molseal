"""Tenant-key rotation: online, in place, resumable."""

import traceback
from uuid import uuid4

import pytest
import sqlalchemy as sa

from molseal import KeyVersionChanged, VersionConflict, keys, rotate_tenant_keys, snapshot
from molseal.crypto import SealError, open_sealed, row_aad
from molseal.keys import exact_key_hmac
from molseal.store import SqlStore, structure_keys, structures
from molseal.vault import Vault, VaultError, rewrap_tenant
from tests.mols import molecules


@pytest.fixture
def env(tmp_path):
    store = SqlStore(sa.create_engine(f"sqlite:///{tmp_path / 'r.db'}"))
    store.create_tables()
    kp = tmp_path / "kek.key"
    keys.generate_key_file(kp)
    return store, keys.FileKeyProvider(kp), tmp_path / "snap"


class _Crash(Exception):
    pass


def _crash(*a):
    raise _Crash


def _loaded(store, kp, n=20, snapshot_dir=None):
    """A tenant with n structures, each with an exact key name=m<j>; ids[0] replaced, ids[1] deleted."""
    t = uuid4()
    v = Vault(store, kp, t, snapshot_dir=snapshot_dir)
    ids = [uuid4() for _ in range(n)]
    v.put_many([(i, s, {"name": f"m{j}"}) for j, (i, s) in enumerate(zip(ids, molecules()[:n]))])
    v.put(ids[0], "CCO", {"name": "m0"})
    v.delete(ids[1])
    return t, v, ids


def _started(store, kp, t, monkeypatch):
    """A rotation that started and re-encrypted nothing (the worker died at its first batch)."""
    monkeypatch.setattr(store, "reseal_structures", _crash)
    with pytest.raises(_Crash):
        rotate_tenant_keys(store, t, kp)
    monkeypatch.undo()


def test_rotation_reencrypts_every_row_and_retires_the_old_keys(env):
    store, kp, _ = env
    t, v, ids = _loaded(store, kp)
    old = v._keys
    before = {r.id: (r.seq, r.row_version, r.deleted) for r in store.changes_since(t, 0)}
    state = store.tenant_state(t)
    hits = set(v.substructure("[#6]", kind="smarts"))

    assert rotate_tenant_keys(store, t, kp, batch=7) == len(before)      # tombstones too; 7 forces several batches

    rows = store.changes_since(t, 0)
    assert {r.id: (r.seq, r.row_version, r.deleted) for r in rows} == before
    assert store.tenant_state(t) == state
    assert {r.data_key_version for r in rows} == {2}
    assert sorted((k.kind, k.version) for k in store.get_tenant_keys(t)) == [("data", 2), ("hmac", 2)]
    for r in rows:                                     # the stolen data key opens nothing
        for version in (1, 2):
            with pytest.raises(SealError):
                open_sealed(old.data_key, r.nonce, r.ciphertext, row_aad(t, r.id, version, r.row_version))
    with store.engine.connect() as conn:
        stored = {h for (h,) in conn.execute(sa.select(structure_keys.c.hmac).where(structure_keys.c.tenant_id == t))}
    assert len(stored) == len(ids) - 1 and exact_key_hmac(old.hmac_key, "name", "m5") not in stored
    fresh = Vault(store, kp, t)
    assert fresh.find_exact("name", "m5") == [ids[5]] and fresh.get(ids[0]).smiles == "CCO" and fresh.get(ids[1]) is None
    assert set(fresh.substructure("[#6]", kind="smarts")) == hits


def test_a_vault_opened_before_the_rotation_keeps_working(env):
    store, kp, _ = env
    t, v, ids = _loaded(store, kp)
    rotate_tenant_keys(store, t, kp)
    assert v.get(ids[2]) is not None and set(v.get_many(ids[2:5])) == set(ids[2:5])
    assert v.find_exact("name", "m3") == [ids[3]]
    new = uuid4()
    v.put(new, "CCN", {"name": "new"})                 # refused under v1 once, then written under v2
    v.put_many([(uuid4(), "CCCl", None)])
    v.delete(ids[4])
    assert {r.data_key_version for r in store.changes_since(t, 0)} == {2}
    fresh = Vault(store, kp, t)
    assert fresh.find_exact("name", "new") == [new] and fresh.get(ids[4]) is None
    assert new in v.substructure("CCN")


def test_a_crash_midway_leaves_every_row_readable_and_a_resume_finishes(env, monkeypatch):
    store, kp, _ = env
    t, _v, ids = _loaded(store, kp)
    real, calls = store.reseal_structures, []

    def flaky(batch):
        calls.append(1)
        if len(calls) == 2:
            raise _Crash
        return real(batch)
    monkeypatch.setattr(store, "reseal_structures", flaky)
    with pytest.raises(_Crash):
        rotate_tenant_keys(store, t, kp, batch=5)
    monkeypatch.undo()
    assert {r.data_key_version for r in store.changes_since(t, 0)} == {1, 2}
    fresh = Vault(store, kp, t)
    assert fresh.find_exact("name", "m2") == [ids[2]] and fresh.find_exact("name", "m15") == [ids[15]]
    assert rotate_tenant_keys(store, t, kp, resume_only=True) > 0
    assert sorted(k.version for k in store.get_tenant_keys(t)) == [2, 2]
    assert rotate_tenant_keys(store, t, kp, resume_only=True) == 0
    assert rotate_tenant_keys(store, t, kp) == len(ids)          # a plain call starts the next rotation
    assert sorted(k.version for k in store.get_tenant_keys(t)) == [3, 3]


def test_resume_only_with_nothing_pending_writes_nothing(env):
    store, kp, _ = env
    t, _v, _ids = _loaded(store, kp)
    before = store.get_tenant_keys(t)
    assert rotate_tenant_keys(store, t, kp, resume_only=True) == 0
    assert store.get_tenant_keys(t) == before
    with pytest.raises(ValueError):
        rotate_tenant_keys(store, t, kp, batch=0)
    assert store.get_tenant_keys(t) == before
    with pytest.raises(VaultError):
        rotate_tenant_keys(store, uuid4(), kp)


def test_a_write_during_the_pass_is_kept_and_already_current(env, monkeypatch):
    store, kp, _ = env
    t, v, ids = _loaded(store, kp)
    real = store.reseal_structures

    def write_first(batch):
        monkeypatch.setattr(store, "reseal_structures", real)
        v.put(ids[3], "CCCC", {"name": "edited"})        # ids[3] is in this batch, read before the edit
        return real(batch)
    monkeypatch.setattr(store, "reseal_structures", write_first)
    rotate_tenant_keys(store, t, kp, batch=5)
    fresh = Vault(store, kp, t)
    assert fresh.get(ids[3]).smiles == "CCCC" and fresh.find_exact("name", "edited") == [ids[3]]
    assert {r.data_key_version for r in store.changes_since(t, 0)} == {2}


def test_find_exact_returns_a_row_resealed_between_its_per_version_queries_once(env, monkeypatch):
    store, kp, _ = env
    t, _v, ids = _loaded(store, kp)
    _started(store, kp, t, monkeypatch)
    mid = Vault(store, kp, t)                            # holds v1 and v2
    real, calls = store.find_by_key, []

    def find_then_finish(*a):
        hits = real(*a)
        if not calls:
            calls.append(1)
            rotate_tenant_keys(store, t, kp, resume_only=True)    # every row moves to v2 between the queries
        return hits
    monkeypatch.setattr(store, "find_by_key", find_then_finish)
    assert mid.find_exact("name", "m7") == [ids[7]]


def test_find_exact_in_a_vault_that_missed_the_whole_rotation(env, monkeypatch):
    store, kp, _ = env
    t, v, ids = _loaded(store, kp)                       # v holds only v1
    real, calls = store.find_by_key, []

    def rotate_first(*a):
        if not calls:
            calls.append(1)
            rotate_tenant_keys(store, t, kp)
        return real(*a)
    monkeypatch.setattr(store, "find_by_key", rotate_first)
    assert v.find_exact("name", "m7") == [ids[7]]


def test_snapshots_across_a_rotation(env, monkeypatch, caplog):
    store, kp, snap = env
    t, v, _ids = _loaded(store, kp, snapshot_dir=snap)
    path = v.save_snapshot()                                        # under v1
    _started(store, kp, t, monkeypatch)
    rotate_tenant_keys(store, t, kp, resume_only=True)
    with caplog.at_level("WARNING", logger="molseal.vault"):
        after = Vault(store, kp, t, snapshot_dir=snap)              # v1 retired: rebuilt from the store
    assert [r for r in caplog.records if "ignoring snapshot" in r.getMessage()]
    assert set(after.substructure("[#6]", kind="smarts")) == set(v.substructure("[#6]", kind="smarts"))
    v.save_snapshot()                                               # v only searched since the rotation
    assert snapshot._header(path.read_bytes())[0]["key_version"] == 2


def test_rewrap_mid_rotation_rewraps_both_versions_and_the_rotation_finishes(env, tmp_path, monkeypatch):
    store, kp, _ = env
    t, _v, ids = _loaded(store, kp)
    _started(store, kp, t, monkeypatch)
    new_path = tmp_path / "new.key"
    keys.generate_key_file(new_path)
    new = keys.FileKeyProvider(new_path)
    assert rewrap_tenant(store, t, kp, new) == 4
    assert rotate_tenant_keys(store, t, new, resume_only=True) == len(ids)
    assert Vault(store, new, t).find_exact("name", "m9") == [ids[9]]


def test_a_rewrap_that_raced_a_rotation_start_changes_nothing(env, monkeypatch):
    store, kp, _ = env
    t, _v, ids = _loaded(store, kp)

    class StartsRotation:
        fired = False

        def wrap(self, label, plaintext):
            if not self.fired:                          # rewrap read the two v1 rows; now a rotation starts
                self.fired = True
                _started(store, kp, t, monkeypatch)
            return kp.wrap(label, plaintext)

        def unwrap(self, label, blob):
            return kp.unwrap(label, blob)
    with pytest.raises(VersionConflict):
        rewrap_tenant(store, t, kp, StartsRotation())
    assert len(store.get_tenant_keys(t)) == 4
    assert rotate_tenant_keys(store, t, kp, resume_only=True) == len(ids)


def test_a_row_that_does_not_open_stops_the_rotation_and_names_only_its_id(env):
    store, kp, _ = env
    t, _v, ids = _loaded(store, kp)
    with store.engine.begin() as conn:
        conn.execute(sa.update(structures).where(structures.c.tenant_id == t, structures.c.id == ids[5])
                     .values(ciphertext=b"\x00" * 300))
    with pytest.raises(VaultError) as e:
        rotate_tenant_keys(store, t, kp)
    text = "".join(traceback.format_exception(e.value))
    assert str(ids[5]) in str(e.value) and molecules()[5] not in text and "m5" not in text
    assert len(store.get_tenant_keys(t)) == 4                        # pending; the old keys stay


def test_key_version_changed_is_a_version_conflict():
    assert issubclass(KeyVersionChanged, VersionConflict)
