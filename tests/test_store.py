from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from molseal.store import KeyVersionChanged, SqlStore, StructureRecord, VersionConflict, WrappedKey, tenants


@pytest.fixture
def store(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'molseal.db'}")
    s = SqlStore(engine)
    s.create_tables()
    return s


def rec(tenant, id_, rv=1, deleted=False, ct=b"ct"):
    return StructureRecord(tenant_id=tenant, id=id_, row_version=rv, data_key_version=1,
                           nonce=b"n" * 12, ciphertext=ct, deleted=deleted)


def test_ensure_tenant_is_idempotent(store):
    t = uuid4()
    first = store.ensure_tenant(t)
    second = store.ensure_tenant(t)
    assert first == second
    assert first.seq_cursor == 0 and first.data_key_version == 1 and first.hmac_key_version == 1


def test_tenant_keys_round_trip(store):
    t = uuid4()
    store.ensure_tenant(t)
    store.put_tenant_keys(t, [WrappedKey("data", 1, b"w1"), WrappedKey("hmac", 1, b"w2")])
    got = {(k.kind, k.version): k.wrapped for k in store.get_tenant_keys(t)}
    assert got == {("data", 1): b"w1", ("hmac", 1): b"w2"}
    assert store.get_tenant_keys(uuid4()) == []


def test_replace_tenant_keys_swaps_blobs_and_leaves_other_tenants_alone(store):
    t, u = uuid4(), uuid4()
    store.ensure_tenant(t)
    store.ensure_tenant(u)
    store.put_tenant_keys(t, [WrappedKey("data", 1, b"w1"), WrappedKey("hmac", 1, b"w2")])
    store.put_tenant_keys(u, [WrappedKey("data", 1, b"u1"), WrappedKey("hmac", 1, b"u2")])
    store.replace_tenant_keys(t, [WrappedKey("data", 1, b"n1"), WrappedKey("hmac", 1, b"n2")])
    assert {(k.kind, k.version): k.wrapped for k in store.get_tenant_keys(t)} == {("data", 1): b"n1", ("hmac", 1): b"n2"}
    assert {k.wrapped for k in store.get_tenant_keys(u)} == {b"u1", b"u2"}


def test_put_structures_refuses_a_row_sealed_under_an_old_key_version(store):
    t = uuid4()
    store.ensure_tenant(t)
    with store.engine.begin() as conn:
        conn.execute(sa.update(tenants).where(tenants.c.tenant_id == t).values(data_key_version=2, hmac_key_version=2))
    with pytest.raises(KeyVersionChanged):
        store.put_structures([(rec(t, uuid4()), {"k": b"h" * 32}, None)])
    assert store.tenant_state(t) == (0, 0) and store.changes_since(t, 0) == []
    assert issubclass(KeyVersionChanged, VersionConflict)


def test_replace_tenant_keys_refuses_a_row_set_that_changed_since_it_was_read(store):
    t = uuid4()
    store.ensure_tenant(t)
    store.put_tenant_keys(t, [WrappedKey("data", 1, b"w1"), WrappedKey("hmac", 1, b"w2")])
    store.put_tenant_keys(t, [WrappedKey("data", 2, b"w3"), WrappedKey("hmac", 2, b"w4")])   # a rotation started
    with pytest.raises(VersionConflict):
        store.replace_tenant_keys(t, [WrappedKey("data", 1, b"n1"), WrappedKey("hmac", 1, b"n2")])
    with pytest.raises(VersionConflict):
        store.replace_tenant_keys(t, [])
    assert {k.wrapped for k in store.get_tenant_keys(t)} == {b"w1", b"w2", b"w3", b"w4"}


def test_list_tenants(store):
    assert store.list_tenants() == []
    ids = {uuid4() for _ in range(3)}
    for i in ids:
        store.ensure_tenant(i)
    assert set(store.list_tenants()) == ids and len(store.list_tenants()) == 3


def test_put_assigns_increasing_seq_and_changes_since_orders_by_it(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    s1 = store.put_structure(rec(t, a), {"inchikey": b"h" * 32}, None)
    s2 = store.put_structure(rec(t, b), {}, None)
    assert (s1, s2) == (1, 2)
    assert store.seq_cursor(t) == 2
    assert [r.id for r in store.changes_since(t, 0)] == [a, b]
    assert [r.id for r in store.changes_since(t, 1)] == [b]
    assert store.changes_since(t, 2) == []
    assert store.max_seq(t) == 2


def test_update_replaces_row_and_key_rows_and_bumps_seq(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    store.put_structure(rec(t, a, rv=1, ct=b"v1"), {"inchikey": b"h" * 32, "other": b"o" * 32}, None)
    s = store.put_structure(rec(t, a, rv=2, ct=b"v2"), {"inchikey": b"k" * 32}, 1)
    assert s == 2
    got = store.get_structure(t, a)
    assert got.row_version == 2 and got.ciphertext == b"v2" and got.seq == 2
    assert store.find_by_key(t, "inchikey", b"h" * 32) == []
    assert store.find_by_key(t, "inchikey", b"k" * 32) == [a]
    assert store.find_by_key(t, "other", b"o" * 32) == []
    assert len(store.changes_since(t, 0)) == 1  # one row, latest state


def test_delete_is_a_write_that_bumps_seq_and_hides_keys(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    store.put_structure(rec(t, a), {"inchikey": b"h" * 32}, None)
    s = store.put_structure(rec(t, a, rv=2, deleted=True), {}, 1)
    assert s == 2
    assert store.tenant_state(t) == (2, 0)
    assert store.find_by_key(t, "inchikey", b"h" * 32) == []
    assert store.changes_since(t, 1)[0].deleted is True


def test_find_by_key_is_tenant_scoped_and_can_return_several(store):
    t1, t2 = uuid4(), uuid4()
    store.ensure_tenant(t1)
    store.ensure_tenant(t2)
    a, b, c = uuid4(), uuid4(), uuid4()
    store.put_structure(rec(t1, a), {"inchikey": b"h" * 32}, None)
    store.put_structure(rec(t1, b), {"inchikey": b"h" * 32}, None)
    store.put_structure(rec(t2, c), {"inchikey": b"h" * 32}, None)
    assert sorted(store.find_by_key(t1, "inchikey", b"h" * 32)) == sorted([a, b])
    assert store.find_by_key(t2, "inchikey", b"h" * 32) == [c]
    assert store.tenant_state(t1) == (2, 2)


def test_get_structure_missing_is_none(store):
    t = uuid4()
    store.ensure_tenant(t)
    assert store.get_structure(t, uuid4()) is None


def test_stale_expected_version_raises_and_writes_nothing(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    store.put_structure(rec(t, a), {}, None)
    store.put_structure(rec(t, a, rv=2, ct=b"v2"), {}, 1)
    with pytest.raises(VersionConflict):
        store.put_structure(rec(t, a, rv=2, ct=b"stale"), {}, 1)     # read rv 1 before the write above
    with pytest.raises(VersionConflict):
        store.put_structure(rec(t, uuid4()), {}, 3)                   # expected a row that is not there
    with pytest.raises(VersionConflict):
        store.put_structure(rec(t, a, rv=1), {}, None)                # expected no row
    assert store.get_structure(t, a).ciphertext == b"v2"
    assert store.tenant_state(t) == (2, 1)


def test_live_count_tracks_every_transition(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    steps = [(1, False, None, 1), (2, False, 1, 1), (3, True, 2, 0), (4, True, 3, 0), (5, False, 4, 1)]
    for rv, deleted, expected, live in steps:   # new, live->live, live->dead, dead->dead, dead->live
        store.put_structure(rec(t, a, rv=rv, deleted=deleted), {}, expected)
        assert store.tenant_state(t)[1] == live


def test_changes_since_upto_bounds_the_range(store):
    t = uuid4()
    store.ensure_tenant(t)
    ids = [uuid4() for _ in range(3)]
    for i in ids:
        store.put_structure(rec(t, i), {}, None)
    assert [r.id for r in store.changes_since(t, 0, upto=2)] == ids[:2]
    assert [r.id for r in store.changes_since(t, 1, upto=3)] == ids[1:]
    assert store.changes_since(t, 2, upto=2) == []


def test_put_structures_assigns_consecutive_seqs_and_counts_live(store):
    t = uuid4()
    store.ensure_tenant(t)
    ids = [uuid4() for _ in range(5)]
    seqs = store.put_structures([(rec(t, i), {"inchikey": bytes([n]) * 32}, None) for n, i in enumerate(ids)])
    assert seqs == [1, 2, 3, 4, 5]
    assert store.tenant_state(t) == (5, 5)
    assert [r.id for r in store.changes_since(t, 0)] == ids
    assert store.find_by_key(t, "inchikey", bytes([2]) * 32) == [ids[2]]
    got = store.get_structures(t, ids[:2] + [uuid4()])
    assert set(got) == set(ids[:2]) and got[ids[0]].seq == 1


def test_put_structures_rejects_duplicate_ids_before_writing(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    with pytest.raises(ValueError):
        store.put_structures([(rec(t, a), {}, None), (rec(t, a), {}, None)])
    assert store.tenant_state(t) == (0, 0)


def test_put_structures_is_all_or_nothing_on_a_stale_version(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    store.put_structure(rec(t, a), {}, None)
    with pytest.raises(VersionConflict):
        store.put_structures([(rec(t, b), {}, None), (rec(t, a, rv=2), {}, 5)])
    assert store.tenant_state(t) == (1, 1)
    assert store.get_structure(t, b) is None


def test_put_structures_updates_existing_rows_and_adjusts_live_count(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    store.put_structures([(rec(t, a), {"k": b"a" * 32}, None), (rec(t, b), {}, None)])
    seqs = store.put_structures([(rec(t, a, rv=2, ct=b"v2"), {"k": b"c" * 32}, 1),
                                 (rec(t, b, rv=2, deleted=True), {}, 1)])
    assert seqs == [3, 4]
    assert store.tenant_state(t) == (4, 1)
    assert store.get_structure(t, a).ciphertext == b"v2"
    assert store.find_by_key(t, "k", b"a" * 32) == [] and store.find_by_key(t, "k", b"c" * 32) == [a]


def test_put_structures_empty_batch_is_a_no_op(store):
    t = uuid4()
    store.ensure_tenant(t)
    assert store.put_structures([]) == []
    assert store.tenant_state(t) == (0, 0)


def test_put_structures_rejects_a_mixed_tenant_batch(store):
    t, other = uuid4(), uuid4()
    store.ensure_tenant(t)
    store.ensure_tenant(other)
    with pytest.raises(ValueError, match="one tenant"):
        store.put_structures([(rec(t, uuid4()), {}, None), (rec(other, uuid4()), {}, None)])
    assert store.tenant_state(t) == (0, 0) and store.tenant_state(other) == (0, 0)


def _keyed(store, t):
    store.ensure_tenant(t)
    store.put_tenant_keys(t, [WrappedKey("data", 1, b"d1"), WrappedKey("hmac", 1, b"h1")])


NEXT = [WrappedKey("data", 2, b"d2"), WrappedKey("hmac", 2, b"h2")]


def test_start_key_rotation_makes_the_next_versions_current_and_refuses_while_pending(store):
    t = uuid4()
    _keyed(store, t)
    assert not store.start_key_rotation(t, [WrappedKey("data", 3, b"x"), WrappedKey("hmac", 3, b"y")])
    assert store.start_key_rotation(t, NEXT)
    row = store.ensure_tenant(t)
    assert (row.data_key_version, row.hmac_key_version) == (2, 2)
    assert not store.start_key_rotation(t, [WrappedKey("data", 3, b"d3"), WrappedKey("hmac", 3, b"h3")])
    assert sorted((k.kind, k.version) for k in store.get_tenant_keys(t)) == [
        ("data", 1), ("data", 2), ("hmac", 1), ("hmac", 2)]


def test_reseal_rewrites_old_rows_in_place_and_skips_changed_ones(store):
    t, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    _keyed(store, t)
    store.put_structures([(rec(t, a), {"k": b"a" * 32}, None), (rec(t, b), {}, None),
                          (rec(t, c, deleted=True), {}, None)])
    seqs = {r.id: r.seq for r in store.changes_since(t, 0)}
    assert store.start_key_rotation(t, NEXT)

    def new(i, deleted=False):
        return replace(rec(t, i, deleted=deleted, ct=b"new"), data_key_version=2)

    with pytest.raises(KeyVersionChanged):
        store.reseal_structures([(rec(t, a), {})])                       # still sealed under v1
    store.put_structures([(replace(rec(t, b, rv=2, ct=b"writer"), data_key_version=2), {}, 1)])
    assert store.reseal_structures([(new(a), {"k": b"A" * 32}), (new(b), {}), (new(c, deleted=True), {})]) == 2
    rows = {r.id: r for r in store.changes_since(t, 0)}
    assert rows[a].data_key_version == 2 and rows[a].ciphertext == b"new" and rows[a].seq == seqs[a]
    assert rows[c].deleted and rows[c].data_key_version == 2 and rows[c].seq == seqs[c]
    assert rows[b].ciphertext == b"writer" and rows[b].row_version == 2      # the writer's row is kept
    assert store.find_by_key(t, "k", b"A" * 32) == [a] and store.find_by_key(t, "k", b"a" * 32) == []
    assert store.tenant_state(t)[1] == 2                                     # live count unchanged
    assert store.reseal_structures([(new(a), {})]) == 0                      # already current


def test_finish_key_rotation_waits_for_the_last_old_row(store):
    t, a = uuid4(), uuid4()
    _keyed(store, t)
    store.put_structures([(rec(t, a), {}, None)])
    assert store.start_key_rotation(t, NEXT)
    assert not store.finish_key_rotation(t)
    assert len(store.get_tenant_keys(t)) == 4
    assert store.reseal_structures([(replace(rec(t, a), data_key_version=2), {})]) == 1
    assert store.finish_key_rotation(t)
    assert sorted((k.kind, k.version) for k in store.get_tenant_keys(t)) == [("data", 2), ("hmac", 2)]
