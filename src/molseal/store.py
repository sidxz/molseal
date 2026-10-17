"""The four tables and a SQLAlchemy Core store. No chemistry lives here.

The change sequence comes from a per-tenant cursor row updated inside the writer's
transaction. The row lock is held to commit, so sequence order equals commit order on
PostgreSQL and SQLite alike. A plain database sequence does not give that guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Integer, LargeBinary, MetaData, String,
                        Table, Uuid, delete, func, insert, select, update)

from .errors import MolsealError

metadata = MetaData()

tenants = Table(
    "tenants", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("seq_cursor", BigInteger, nullable=False),
    Column("data_key_version", Integer, nullable=False),
    Column("hmac_key_version", Integer, nullable=False),
    Column("live_count", BigInteger, nullable=False),   # maintained by put_structure under the cursor lock
)

tenant_keys = Table(
    "tenant_keys", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("kind", String(8), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("wrapped", LargeBinary, nullable=False),   # provider-formatted; the label is bound inside
)

structures = Table(
    "structures", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("row_version", Integer, nullable=False),
    Column("data_key_version", Integer, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("deleted", Boolean, nullable=False),   # index only; the authenticated flag is in the payload
    Column("seq", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    sa.Index("ix_structures_tenant_seq", "tenant_id", "seq"),
)

structure_keys = Table(
    "structure_keys", metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("id", Uuid, primary_key=True),
    Column("kind", String(64), primary_key=True),
    Column("hmac", LargeBinary(32), nullable=False),   # index only; the authenticated keys are in the payload
    sa.Index("ix_structure_keys_lookup", "tenant_id", "kind", "hmac"),
)


@dataclass(frozen=True)
class TenantRow:
    """The tenant's cursor and current key versions, as read when a Vault opens."""

    tenant_id: UUID
    seq_cursor: int
    data_key_version: int
    hmac_key_version: int


@dataclass(frozen=True)
class WrappedKey:
    """One tenant key row: `kind` is 'data' or 'hmac', `wrapped` is the provider's opaque blob."""

    kind: str
    version: int
    wrapped: bytes


@dataclass(frozen=True)
class StructureRecord:
    """One structures row as stored: ciphertext and the plaintext columns the label binds."""

    tenant_id: UUID
    id: UUID
    row_version: int
    data_key_version: int
    nonce: bytes
    ciphertext: bytes
    deleted: bool
    seq: int = 0


class VersionConflict(MolsealError):
    """The stored state changed since the caller read it: another row_version, or another set of key rows."""


class KeyVersionChanged(VersionConflict):
    """A write was sealed under a data key version that is no longer the tenant's current one: a key rotation
    started after the writer loaded its keys. The vault reloads its keys and retries."""


class StructureStore(Protocol):
    """What a Vault needs from storage. SqlStore is the SQLAlchemy implementation; another backend must
    keep three guarantees: `put_structure(s)` assigns sequence numbers in commit order under a per-tenant
    lock; every write is compare-and-set on the row version; and, under that same lock, a record sealed
    under a data key version other than the tenant's current one is refused with KeyVersionChanged.
    Without the third, a writer that opened before a key rotation keeps writing rows under a key the
    rotation then deletes."""

    def create_tables(self) -> None:
        """Create any missing tables; a no-op when they exist."""

    def ensure_tenant(self, tenant_id: UUID) -> TenantRow:
        """The tenant row, created with cursor 0 and key versions 1 when absent."""

    def seq_cursor(self, tenant_id: UUID) -> int:
        """The last sequence number handed out for the tenant."""

    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]:
        """Every wrapped key row of the tenant, ordered by kind then version."""

    def put_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None:
        """Insert key rows; a duplicate (kind, version) is an error."""

    def replace_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None:
        """Under the tenant lock, replace the blobs of the tenant's key rows. `keys` must name exactly the
        (kind, version) rows present; otherwise raise VersionConflict and write nothing."""

    def list_tenants(self) -> list[UUID]:
        """Every tenant id, sorted."""

    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes],
                      expected_row_version: int | None) -> int:
        """Write one row; see put_structures. Returns its sequence number."""

    def put_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes], int | None]]) -> list[int]:
        """Write a batch of one tenant's rows in one transaction, each compared against the row version
        the caller read (None: no row). Raises VersionConflict and writes nothing on any mismatch, and
        KeyVersionChanged if a record is not sealed under the tenant's current data key version.
        Returns consecutive, commit-ordered sequence numbers."""

    def get_structure(self, tenant_id: UUID, id: UUID) -> StructureRecord | None:
        """One row, live or tombstoned, or None when the id was never written."""

    def get_structures(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, StructureRecord]:
        """The rows among `ids` that exist, keyed by id."""

    def changes_since(self, tenant_id: UUID, seq: int, limit: int = 10000,
                      upto: int | None = None) -> list[StructureRecord]:
        """Rows with sequence number above `seq` (and at most `upto`), ascending, at most `limit`."""

    def tenant_state(self, tenant_id: UUID) -> tuple[int, int]:
        """(seq_cursor, live_count) read together, so the count is exact at that cursor."""

    def max_seq(self, tenant_id: UUID) -> int:
        """The highest sequence number among the tenant's rows, 0 when there are none."""

    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]:
        """Ids of live rows whose exact key `kind` hashes to `hmac`."""

    def start_key_rotation(self, tenant_id: UUID, keys: list[WrappedKey]) -> bool:
        """Under the tenant lock: unless some key kind already has two versions (a rotation is pending), insert
        `keys`, which must be the data and hmac keys one version above the current ones, and make them current.
        Otherwise write nothing and return False."""

    def reseal_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes]]]) -> int:
        """Re-encrypt one tenant's rows in place for a key rotation. Under the tenant lock, each row whose stored
        row version still equals the record's and whose stored data key version is below the current one takes
        the record's nonce, ciphertext and data key version, and its exact-key hashes. The other rows were
        rewritten under the current key since they were read and are skipped. Sequence numbers, row versions
        and the live count do not change. Raises KeyVersionChanged, writing nothing, if a record is not sealed
        under the current version. Returns how many rows were rewritten."""

    def finish_key_rotation(self, tenant_id: UUID) -> bool:
        """Under the tenant lock: if no row is sealed under a data key version below the current one, delete the
        key rows below the current versions and return True; otherwise delete nothing and return False."""


def _record(row: sa.Row[Any]) -> StructureRecord:
    return StructureRecord(
        tenant_id=row.tenant_id, id=row.id, row_version=row.row_version,
        data_key_version=row.data_key_version, nonce=row.nonce, ciphertext=row.ciphertext,
        deleted=bool(row.deleted), seq=row.seq,
    )


def _key_rows(tenant_id: UUID, keys: list[WrappedKey]) -> list[dict[str, Any]]:
    return [dict(tenant_id=tenant_id, kind=k.kind, version=k.version, wrapped=k.wrapped) for k in keys]


def _lock_tenant(conn: sa.Connection, tenant_id: UUID) -> sa.Row[Any]:
    """Take the tenant row lock for the rest of the transaction and return the current key versions. The
    UPDATE changes nothing; it holds the row lock to commit on PostgreSQL and the write lock on SQLite."""
    return conn.execute(update(tenants).where(tenants.c.tenant_id == tenant_id)
                        .values(seq_cursor=tenants.c.seq_cursor)
                        .returning(tenants.c.data_key_version, tenants.c.hmac_key_version)).one()


class SqlStore:
    """StructureStore on SQLAlchemy Core. Tested on PostgreSQL and SQLite; any backend with RETURNING
    and row locks on UPDATE should work."""

    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine

    def create_tables(self) -> None:
        metadata.create_all(self.engine)

    def ensure_tenant(self, tenant_id: UUID) -> TenantRow:
        with self.engine.begin() as conn:
            row = conn.execute(select(tenants).where(tenants.c.tenant_id == tenant_id)).first()
            if row is None:
                conn.execute(insert(tenants).values(tenant_id=tenant_id, seq_cursor=0,
                                                    data_key_version=1, hmac_key_version=1, live_count=0))
                return TenantRow(tenant_id, 0, 1, 1)
            return TenantRow(row.tenant_id, row.seq_cursor, row.data_key_version, row.hmac_key_version)

    def seq_cursor(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(tenants.c.seq_cursor).where(tenants.c.tenant_id == tenant_id)).scalar_one())

    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tenant_keys).where(tenant_keys.c.tenant_id == tenant_id)
                                .order_by(tenant_keys.c.kind, tenant_keys.c.version)).all()
        return [WrappedKey(r.kind, r.version, r.wrapped) for r in rows]

    def put_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(tenant_keys), _key_rows(tenant_id, keys))

    def replace_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None:
        """Under the tenant lock, swap the blobs of exactly the rows present. A rewrap that read the rows
        before a key rotation started or finished would otherwise delete the new rows or revive old ones."""
        with self.engine.begin() as conn:
            _lock_tenant(conn, tenant_id)
            present = {(r.kind, r.version) for r in conn.execute(
                select(tenant_keys.c.kind, tenant_keys.c.version).where(tenant_keys.c.tenant_id == tenant_id))}
            if present != {(k.kind, k.version) for k in keys} or len(keys) != len(present):
                raise VersionConflict(f"tenant {tenant_id}: key rows changed since they were read; run again")
            for k in keys:
                conn.execute(update(tenant_keys)
                             .where(tenant_keys.c.tenant_id == tenant_id, tenant_keys.c.kind == k.kind,
                                    tenant_keys.c.version == k.version)
                             .values(wrapped=k.wrapped))

    def list_tenants(self) -> list[UUID]:
        with self.engine.connect() as conn:
            return [r.tenant_id for r in conn.execute(select(tenants.c.tenant_id).order_by(tenants.c.tenant_id)).all()]

    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes],
                      expected_row_version: int | None) -> int:
        return self.put_structures([(rec, key_hmacs, expected_row_version)])[0]

    def get_structures(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, StructureRecord]:
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(structures)
                                .where(structures.c.tenant_id == tenant_id, structures.c.id.in_(ids))).all()
        return {r.id: _record(r) for r in rows}

    def put_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes], int | None]]) -> list[int]:
        """One transaction for a whole batch: the cursor advances by the batch size (holding the tenant
        lock to commit, so the numbers stay consecutive and commit-ordered), every row is compare-and-set
        against the version the caller read (None: no row), new rows are bulk-inserted, existing rows
        updated, the live count adjusted once, and key rows replaced. Any conflict raises VersionConflict
        and rolls the whole batch back; a record sealed under another data key version than the tenant's
        raises KeyVersionChanged. The cursor lock serializes every writer of the tenant, so the versions
        read below cannot go stale before commit."""
        if not batch:
            return []
        ids = [rec.id for rec, _, _ in batch]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in batch")
        tenant_id = batch[0][0].tenant_id
        if any(rec.tenant_id != tenant_id for rec, _, _ in batch):
            raise ValueError("a batch holds one tenant's rows")
        n = len(batch)
        with self.engine.begin() as conn:
            row = conn.execute(
                update(tenants).where(tenants.c.tenant_id == tenant_id)
                .values(seq_cursor=tenants.c.seq_cursor + n)
                .returning(tenants.c.seq_cursor, tenants.c.data_key_version)
            ).one()
            # only the data version: both move in one transaction, so a current data version means a current HMAC
            if any(rec.data_key_version != row.data_key_version for rec, _, _ in batch):
                raise KeyVersionChanged(f"tenant {tenant_id}: batch sealed under an old data key version; "
                                        f"the tenant is at {row.data_key_version}")
            first = int(row.seq_cursor) - n + 1
            cur = {r.id: (r.row_version, bool(r.deleted)) for r in conn.execute(
                select(structures.c.id, structures.c.row_version, structures.c.deleted)
                .where(structures.c.tenant_id == tenant_id, structures.c.id.in_(ids)))}
            for rec, _, expected in batch:
                found = cur[rec.id][0] if rec.id in cur else None
                if found != expected:
                    raise VersionConflict(f"{rec.id}: expected row_version {expected}, found {found}")
            delta = sum(int(not rec.deleted) - int(rec.id in cur and not cur[rec.id][1]) for rec, _, _ in batch)
            if delta:
                conn.execute(update(tenants).where(tenants.c.tenant_id == tenant_id)
                             .values(live_count=tenants.c.live_count + delta))
            now = datetime.now(UTC)
            inserts, seqs = [], []
            for offset, (rec, _, _) in enumerate(batch):
                seq = first + offset
                seqs.append(seq)
                values = dict(row_version=rec.row_version, data_key_version=rec.data_key_version,
                              nonce=rec.nonce, ciphertext=rec.ciphertext, deleted=rec.deleted, seq=seq,
                              updated_at=now)
                if rec.id in cur:
                    conn.execute(update(structures)
                                 .where(structures.c.tenant_id == tenant_id, structures.c.id == rec.id)
                                 .values(**values))
                else:
                    inserts.append(dict(tenant_id=tenant_id, id=rec.id, **values))
            if inserts:
                conn.execute(insert(structures), inserts)
            conn.execute(delete(structure_keys)
                         .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.id.in_(ids)))
            key_rows = [dict(tenant_id=tenant_id, id=rec.id, kind=kind, hmac=h)
                        for rec, hmacs, _ in batch for kind, h in hmacs.items()]
            if key_rows:
                conn.execute(insert(structure_keys), key_rows)
            return seqs

    def get_structure(self, tenant_id: UUID, id: UUID) -> StructureRecord | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(structures)
                               .where(structures.c.tenant_id == tenant_id, structures.c.id == id)).first()
        return None if row is None else _record(row)

    def changes_since(self, tenant_id: UUID, seq: int, limit: int = 10000,
                      upto: int | None = None) -> list[StructureRecord]:
        q = select(structures).where(structures.c.tenant_id == tenant_id, structures.c.seq > seq)
        if upto is not None:
            q = q.where(structures.c.seq <= upto)
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(structures.c.seq).limit(limit)).all()
        return [_record(r) for r in rows]

    def tenant_state(self, tenant_id: UUID) -> tuple[int, int]:
        """(seq_cursor, live_count), read together so the count is exact at that cursor."""
        with self.engine.connect() as conn:
            row = conn.execute(select(tenants.c.seq_cursor, tenants.c.live_count)
                               .where(tenants.c.tenant_id == tenant_id)).one()
        return int(row.seq_cursor), int(row.live_count)

    def max_seq(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.coalesce(func.max(structures.c.seq), 0))
                                    .where(structures.c.tenant_id == tenant_id)).scalar_one())

    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]:
        j = structure_keys.join(structures, sa.and_(structures.c.tenant_id == structure_keys.c.tenant_id,
                                                    structures.c.id == structure_keys.c.id))
        with self.engine.connect() as conn:
            rows = conn.execute(select(structure_keys.c.id).select_from(j)
                                .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.kind == kind,
                                       structure_keys.c.hmac == hmac, structures.c.deleted == sa.false())).all()
        return [r.id for r in rows]

    def start_key_rotation(self, tenant_id: UUID, keys: list[WrappedKey]) -> bool:
        with self.engine.begin() as conn:
            t = _lock_tenant(conn, tenant_id)
            kinds = [r.kind for r in conn.execute(select(tenant_keys.c.kind).where(tenant_keys.c.tenant_id == tenant_id))]
            nxt = {("data", t.data_key_version + 1), ("hmac", t.hmac_key_version + 1)}
            if len(kinds) != len(set(kinds)) or len(keys) != 2 or {(k.kind, k.version) for k in keys} != nxt:
                return False
            conn.execute(insert(tenant_keys), _key_rows(tenant_id, keys))
            conn.execute(update(tenants).where(tenants.c.tenant_id == tenant_id)
                         .values(data_key_version=t.data_key_version + 1, hmac_key_version=t.hmac_key_version + 1))
            return True

    def reseal_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes]]]) -> int:
        if not batch:
            return 0
        tenant_id = batch[0][0].tenant_id
        if any(rec.tenant_id != tenant_id for rec, _ in batch):
            raise ValueError("a batch holds one tenant's rows")
        ids = [rec.id for rec, _ in batch]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in batch")
        with self.engine.begin() as conn:
            current = _lock_tenant(conn, tenant_id).data_key_version
            if any(rec.data_key_version != current for rec, _ in batch):
                raise KeyVersionChanged(f"tenant {tenant_id}: reseal batch is not under the current data key "
                                        f"version {current}")
            stored = {r.id: (r.row_version, r.data_key_version) for r in conn.execute(
                select(structures.c.id, structures.c.row_version, structures.c.data_key_version)
                .where(structures.c.tenant_id == tenant_id, structures.c.id.in_(ids)))}
            todo = [(rec, h) for rec, h in batch
                    if rec.id in stored and stored[rec.id][0] == rec.row_version and stored[rec.id][1] < current]
            if not todo:
                return 0
            conn.execute(update(structures)
                         .where(structures.c.tenant_id == sa.bindparam("t"), structures.c.id == sa.bindparam("i"))
                         .values(data_key_version=sa.bindparam("dv"), nonce=sa.bindparam("n"),
                                 ciphertext=sa.bindparam("ct")),
                         [dict(t=tenant_id, i=rec.id, dv=rec.data_key_version, n=rec.nonce, ct=rec.ciphertext)
                          for rec, _ in todo])
            done = [rec.id for rec, _ in todo]
            conn.execute(delete(structure_keys)
                         .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.id.in_(done)))
            key_rows = [dict(tenant_id=tenant_id, id=rec.id, kind=kind, hmac=h)
                        for rec, hmacs in todo for kind, h in hmacs.items()]
            if key_rows:
                conn.execute(insert(structure_keys), key_rows)
            return len(todo)

    def finish_key_rotation(self, tenant_id: UUID) -> bool:
        with self.engine.begin() as conn:
            t = _lock_tenant(conn, tenant_id)
            left = conn.execute(select(structures.c.id).where(
                structures.c.tenant_id == tenant_id, structures.c.data_key_version < t.data_key_version).limit(1)).first()
            if left is not None:
                return False
            conn.execute(delete(tenant_keys).where(tenant_keys.c.tenant_id == tenant_id, sa.or_(
                sa.and_(tenant_keys.c.kind == "data", tenant_keys.c.version < t.data_key_version),
                sa.and_(tenant_keys.c.kind == "hmac", tenant_keys.c.version < t.hmac_key_version))))
            return True
