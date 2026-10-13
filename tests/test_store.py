from uuid import uuid4

import pytest
import sqlalchemy as sa

from molseal.store import SqlStore, StructureRecord, WrappedKey


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
    store.put_tenant_key(t, WrappedKey("data", 1, b"n" * 12, b"w1"))
    store.put_tenant_key(t, WrappedKey("hmac", 1, b"m" * 12, b"w2"))
    got = {(k.kind, k.version): k.wrapped for k in store.get_tenant_keys(t)}
    assert got == {("data", 1): b"w1", ("hmac", 1): b"w2"}
    assert store.get_tenant_keys(uuid4()) == []


def test_put_assigns_increasing_seq_and_changes_since_orders_by_it(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    s1 = store.put_structure(rec(t, a), {"inchikey": b"h" * 32})
    s2 = store.put_structure(rec(t, b), {})
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
    store.put_structure(rec(t, a, rv=1, ct=b"v1"), {"inchikey": b"h" * 32, "other": b"o" * 32})
    s = store.put_structure(rec(t, a, rv=2, ct=b"v2"), {"inchikey": b"k" * 32})
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
    store.put_structure(rec(t, a), {"inchikey": b"h" * 32})
    s = store.put_structure(rec(t, a, rv=2, deleted=True), {})
    assert s == 2
    assert store.live_count(t) == 0
    assert store.find_by_key(t, "inchikey", b"h" * 32) == []
    assert store.changes_since(t, 1)[0].deleted is True


def test_find_by_key_is_tenant_scoped_and_can_return_several(store):
    t1, t2 = uuid4(), uuid4()
    store.ensure_tenant(t1)
    store.ensure_tenant(t2)
    a, b, c = uuid4(), uuid4(), uuid4()
    store.put_structure(rec(t1, a), {"inchikey": b"h" * 32})
    store.put_structure(rec(t1, b), {"inchikey": b"h" * 32})
    store.put_structure(rec(t2, c), {"inchikey": b"h" * 32})
    assert sorted(store.find_by_key(t1, "inchikey", b"h" * 32)) == sorted([a, b])
    assert store.find_by_key(t2, "inchikey", b"h" * 32) == [c]
    assert store.live_count(t1) == 2


def test_get_structure_missing_is_none(store):
    t = uuid4()
    store.ensure_tenant(t)
    assert store.get_structure(t, uuid4()) is None
