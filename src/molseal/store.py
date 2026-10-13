"""The four tables and a SQLAlchemy Core store. No chemistry lives here.

The change sequence comes from a per-tenant cursor row updated inside the writer's
transaction. The row lock is held to commit, so sequence order equals commit order on
PostgreSQL and SQLite alike. A plain database sequence does not give that guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Integer, LargeBinary, MetaData, String,
                        Table, Uuid, delete, func, insert, select, update)

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
    Column("nonce", LargeBinary, nullable=False),
    Column("wrapped", LargeBinary, nullable=False),
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
    tenant_id: UUID
    seq_cursor: int
    data_key_version: int
    hmac_key_version: int


@dataclass(frozen=True)
class WrappedKey:
    kind: str
    version: int
    nonce: bytes
    wrapped: bytes


@dataclass(frozen=True)
class StructureRecord:
    tenant_id: UUID
    id: UUID
    row_version: int
    data_key_version: int
    nonce: bytes
    ciphertext: bytes
    deleted: bool
    seq: int = 0


class VersionConflict(Exception):
    """put_structure found a row_version other than the one the caller read."""


class StructureStore(Protocol):
    def ensure_tenant(self, tenant_id: UUID) -> TenantRow: ...
    def seq_cursor(self, tenant_id: UUID) -> int: ...
    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]: ...
    def put_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None: ...
    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes],
                      expected_row_version: int | None) -> int: ...
    def get_structure(self, tenant_id: UUID, id: UUID) -> StructureRecord | None: ...
    def changes_since(self, tenant_id: UUID, seq: int, limit: int = 10000,
                      upto: int | None = None) -> list[StructureRecord]: ...
    def tenant_state(self, tenant_id: UUID) -> tuple[int, int]: ...
    def max_seq(self, tenant_id: UUID) -> int: ...
    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]: ...


def _record(row) -> StructureRecord:
    return StructureRecord(
        tenant_id=row.tenant_id, id=row.id, row_version=row.row_version,
        data_key_version=row.data_key_version, nonce=row.nonce, ciphertext=row.ciphertext,
        deleted=bool(row.deleted), seq=row.seq,
    )


class SqlStore:
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
            return conn.execute(select(tenants.c.seq_cursor).where(tenants.c.tenant_id == tenant_id)).scalar_one()

    def get_tenant_keys(self, tenant_id: UUID) -> list[WrappedKey]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tenant_keys).where(tenant_keys.c.tenant_id == tenant_id)
                                .order_by(tenant_keys.c.kind, tenant_keys.c.version)).all()
        return [WrappedKey(r.kind, r.version, r.nonce, r.wrapped) for r in rows]

    def put_tenant_keys(self, tenant_id: UUID, keys: list[WrappedKey]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(tenant_keys), [dict(tenant_id=tenant_id, kind=k.kind, version=k.version,
                                                    nonce=k.nonce, wrapped=k.wrapped) for k in keys])

    def put_structure(self, rec: StructureRecord, key_hmacs: dict[str, bytes],
                      expected_row_version: int | None) -> int:
        """One transaction: bump the tenant cursor (row lock to commit), compare-and-set on row_version,
        upsert the row, adjust the tenant's live count, replace its keys.

        expected_row_version is what the caller read (None: no row). Any other current value raises
        VersionConflict and the transaction rolls back. The cursor lock serializes every writer of the
        tenant, so the read below cannot go stale before commit."""
        with self.engine.begin() as conn:
            seq = conn.execute(
                update(tenants).where(tenants.c.tenant_id == rec.tenant_id)
                .values(seq_cursor=tenants.c.seq_cursor + 1).returning(tenants.c.seq_cursor)
            ).scalar_one()
            cur = conn.execute(select(structures.c.row_version, structures.c.deleted)
                               .where(structures.c.tenant_id == rec.tenant_id, structures.c.id == rec.id)).first()
            if (None if cur is None else cur.row_version) != expected_row_version:
                raise VersionConflict(f"{rec.id}: expected row_version {expected_row_version}, "
                                      f"found {None if cur is None else cur.row_version}")
            delta = int(not rec.deleted) - int(cur is not None and not cur.deleted)
            if delta:
                conn.execute(update(tenants).where(tenants.c.tenant_id == rec.tenant_id)
                             .values(live_count=tenants.c.live_count + delta))
            values = dict(row_version=rec.row_version, data_key_version=rec.data_key_version,
                          nonce=rec.nonce, ciphertext=rec.ciphertext, deleted=rec.deleted, seq=seq,
                          updated_at=datetime.now(timezone.utc))
            if cur is not None:
                conn.execute(update(structures)
                             .where(structures.c.tenant_id == rec.tenant_id, structures.c.id == rec.id)
                             .values(**values))
            else:
                conn.execute(insert(structures).values(tenant_id=rec.tenant_id, id=rec.id, **values))
            conn.execute(delete(structure_keys)
                         .where(structure_keys.c.tenant_id == rec.tenant_id, structure_keys.c.id == rec.id))
            if key_hmacs:
                conn.execute(insert(structure_keys), [
                    dict(tenant_id=rec.tenant_id, id=rec.id, kind=kind, hmac=h) for kind, h in key_hmacs.items()
                ])
            return int(seq)

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

    def live_count(self, tenant_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count())
                                    .where(structures.c.tenant_id == tenant_id, structures.c.deleted == sa.false())
                                    ).scalar_one())

    def find_by_key(self, tenant_id: UUID, kind: str, hmac: bytes) -> list[UUID]:
        j = structure_keys.join(structures, sa.and_(structures.c.tenant_id == structure_keys.c.tenant_id,
                                                    structures.c.id == structure_keys.c.id))
        with self.engine.connect() as conn:
            rows = conn.execute(select(structure_keys.c.id).select_from(j)
                                .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.kind == kind,
                                       structure_keys.c.hmac == hmac, structures.c.deleted == sa.false())).all()
        return [r.id for r in rows]
