import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, update

from molseal.store import StructureRecord, VersionConflict, WrappedKey, structures, tenants


def _rec(t, i, rv=1):
    return StructureRecord(tenant_id=t, id=i, row_version=rv, data_key_version=1,
                           nonce=b"n" * 12, ciphertext=b"ct", deleted=False)


def test_second_writer_waits_for_first_to_commit_so_seq_order_is_commit_order(pg_store, pg_engine):
    t = uuid4()
    pg_store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    seq_b = []

    # Writer A: take the cursor and hold the transaction open.
    conn_a = pg_engine.connect()
    tx_a = conn_a.begin()
    seq_a = conn_a.execute(update(tenants).where(tenants.c.tenant_id == t)
                           .values(seq_cursor=tenants.c.seq_cursor + 1)
                           .returning(tenants.c.seq_cursor)).scalar_one()
    conn_a.execute(insert(structures).values(tenant_id=t, id=a, row_version=1, data_key_version=1,
                                             nonce=b"n" * 12, ciphertext=b"ct", deleted=False, seq=seq_a,
                                             updated_at=datetime.now(timezone.utc)))

    # Writer B: a normal put, which must block on A's row lock.
    th = threading.Thread(target=lambda: seq_b.append(pg_store.put_structure(_rec(t, b), {}, None)))
    th.start()
    time.sleep(0.5)
    assert th.is_alive(), "writer B should be blocked until A commits"
    assert pg_store.changes_since(t, 0) == [], "a reader sees nothing while A is uncommitted"

    tx_a.commit()
    conn_a.close()
    th.join(timeout=5)
    assert not th.is_alive()
    assert seq_b == [seq_a + 1]
    assert [r.id for r in pg_store.changes_since(t, 0)] == [a, b]
    assert pg_store.seq_cursor(t) == seq_a + 1


def test_round_trip_on_postgres(pg_store):
    t = uuid4()
    pg_store.ensure_tenant(t)
    i = uuid4()
    s = pg_store.put_structure(_rec(t, i), {"inchikey": b"h" * 32}, None)
    got = pg_store.get_structure(t, i)
    assert got.seq == s and got.ciphertext == b"ct"
    assert pg_store.find_by_key(t, "inchikey", b"h" * 32) == [i]


def test_second_writer_with_the_same_read_version_gets_a_conflict(pg_store, pg_engine):
    t = uuid4()
    pg_store.ensure_tenant(t)
    a = uuid4()
    pg_store.put_structure(_rec(t, a), {}, None)
    assert pg_store.get_structure(t, a).row_version == 1      # both writers read rv 1
    outcome = []

    # Writer A: mid-put, holding the cursor lock, row already moved to rv 2.
    conn_a = pg_engine.connect()
    tx_a = conn_a.begin()
    conn_a.execute(update(tenants).where(tenants.c.tenant_id == t).values(seq_cursor=tenants.c.seq_cursor + 1))
    conn_a.execute(update(structures).where(structures.c.tenant_id == t, structures.c.id == a)
                   .values(row_version=2, ciphertext=b"A"))

    def writer_b():
        try:
            outcome.append(pg_store.put_structure(
                StructureRecord(t, a, 2, 1, b"n" * 12, b"B", False), {}, 1))
        except VersionConflict as exc:
            outcome.append(exc)

    th = threading.Thread(target=writer_b)
    th.start()
    time.sleep(0.5)
    assert th.is_alive(), "writer B should be blocked on A's cursor lock"
    tx_a.commit()
    conn_a.close()
    th.join(timeout=5)
    assert not th.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], VersionConflict)
    got = pg_store.get_structure(t, a)
    assert got.row_version == 2 and got.ciphertext == b"A"
    assert pg_store.tenant_state(t) == (2, 1)


def test_a_batch_holds_the_cursor_lock_so_a_single_writer_lands_after_it(pg_store, pg_engine):
    t = uuid4()
    pg_store.ensure_tenant(t)
    ids = [uuid4() for _ in range(3)]
    other = uuid4()
    got = {}

    conn_a = pg_engine.connect()
    tx_a = conn_a.begin()
    end = conn_a.execute(update(tenants).where(tenants.c.tenant_id == t)
                         .values(seq_cursor=tenants.c.seq_cursor + 3)
                         .returning(tenants.c.seq_cursor)).scalar_one()
    assert end == 3

    th = threading.Thread(target=lambda: got.setdefault("seq", pg_store.put_structure(_rec(t, other), {}, None)))
    th.start()
    time.sleep(0.5)
    assert th.is_alive()
    tx_a.rollback()
    conn_a.close()
    th.join(timeout=5)
    assert got["seq"] == 1, "the rolled-back batch consumed nothing; the single writer got the next number"
    seqs = pg_store.put_structures([(_rec(t, i), {}, None) for i in ids])
    assert seqs == [2, 3, 4]


def test_tenant_keys_replace_round_trip_pg(pg_store):
    t = uuid4()
    pg_store.ensure_tenant(t)
    pg_store.put_tenant_keys(t, [WrappedKey("data", 1, b"w1"), WrappedKey("hmac", 1, b"w2")])
    pg_store.replace_tenant_keys(t, [WrappedKey("data", 1, b"n1"), WrappedKey("hmac", 1, b"n2")])
    assert {k.wrapped for k in pg_store.get_tenant_keys(t)} == {b"n1", b"n2"}
    assert t in pg_store.list_tenants()
