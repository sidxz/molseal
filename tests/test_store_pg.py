import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, update

from molseal.store import StructureRecord, structures, tenants


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
    th = threading.Thread(target=lambda: seq_b.append(pg_store.put_structure(_rec(t, b), {})))
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
    s = pg_store.put_structure(_rec(t, i), {"inchikey": b"h" * 32})
    got = pg_store.get_structure(t, i)
    assert got.seq == s and got.ciphertext == b"ct"
    assert pg_store.find_by_key(t, "inchikey", b"h" * 32) == [i]
