"""The per-tenant facade. The only module that holds keys, store and engine together.

Consistency model (single process): a put or delete updates the index in the same call, so a
search right after sees it. Every search first reads the tenant's cursor; if it moved, the vault
pulls the new rows first. If it moved backwards, or the live counts disagree, the index is rebuilt.
"""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID

from . import snapshot
from .crypto import open_sealed, pad, row_aad, seal, unpad
from .engine import QueryKind, StructureIndex, canonicalize
from .keys import KeyProvider, TenantKeys, exact_key_hmac, new_tenant_secret, unwrap_key, wrap_key
from .payload import Payload, decode, encode
from .store import StructureRecord, StructureStore, WrappedKey


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class Vault:
    def __init__(self, store: StructureStore, key_provider: KeyProvider, tenant_id: UUID,
                 snapshot_dir: Path | None = None, num_threads: int = -1, refresh_batch: int = 10000) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.snapshot_dir = None if snapshot_dir is None else Path(snapshot_dir)
        self.num_threads = num_threads
        self.refresh_batch = refresh_batch
        self._lock = threading.Lock()   # ponytail: one lock for reads and writes; RW lock when readers contend
        self._kek = key_provider.kek()
        self.tenant = store.ensure_tenant(tenant_id)
        self.keys = self._load_or_create_keys()
        self.index = self._load_snapshot() or StructureIndex()
        self.refresh()

    # ----- keys -----

    def _load_or_create_keys(self) -> TenantKeys:
        dv, hv = self.tenant.data_key_version, self.tenant.hmac_key_version
        wrapped = {(k.kind, k.version): k for k in self.store.get_tenant_keys(self.tenant_id)}
        if not wrapped:
            data, hm = new_tenant_secret(), new_tenant_secret()
            n, w = wrap_key(self._kek, self.tenant_id, "data", dv, data)
            self.store.put_tenant_key(self.tenant_id, WrappedKey("data", dv, n, w))
            n, w = wrap_key(self._kek, self.tenant_id, "hmac", hv, hm)
            self.store.put_tenant_key(self.tenant_id, WrappedKey("hmac", hv, n, w))
            return TenantKeys(data, dv, hm, hv)
        try:
            d, h = wrapped[("data", dv)], wrapped[("hmac", hv)]
        except KeyError as exc:
            raise VaultError("tenant key rows are missing for the current key versions") from exc
        return TenantKeys(
            unwrap_key(self._kek, self.tenant_id, "data", dv, d.nonce, d.wrapped), dv,
            unwrap_key(self._kek, self.tenant_id, "hmac", hv, h.nonce, h.wrapped), hv,
        )

    # ----- snapshot -----

    def _snapshot_path(self) -> Path | None:
        return None if self.snapshot_dir is None else self.snapshot_dir / f"{self.tenant_id}.molseal"

    def _load_snapshot(self) -> StructureIndex | None:
        p = self._snapshot_path()
        if p is None or not p.exists():
            return None
        return snapshot.load(p, self.keys.data_key, self.tenant_id, self.keys.data_key_version)

    def save_snapshot(self) -> Path:
        p = self._snapshot_path()
        if p is None:
            raise VaultError("no snapshot_dir configured")
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            snapshot.save(self.index, p, self.keys.data_key, self.tenant_id, self.keys.data_key_version)
        return p

    # ----- row crypto -----

    def _open(self, rec: StructureRecord) -> Payload:
        aad = row_aad(self.tenant_id, rec.id, rec.data_key_version, rec.row_version)
        return decode(unpad(open_sealed(self.keys.data_key, rec.nonce, rec.ciphertext, aad)))

    def _seal(self, id: UUID, row_version: int, payload: Payload) -> tuple[bytes, bytes]:
        aad = row_aad(self.tenant_id, id, self.keys.data_key_version, row_version)
        return seal(self.keys.data_key, pad(encode(payload)), aad)

    # ----- writes -----

    def put(self, id: UUID, smiles: str, exact_keys: dict[str, str] | None = None) -> int:
        canonical = canonicalize(smiles)          # raises StructureError before any write
        ek = dict(exact_keys or {})
        with self._lock:
            existing = self.store.get_structure(self.tenant_id, id)
            rv = 1 if existing is None else existing.row_version + 1
            nonce, ct = self._seal(id, rv, Payload(canonical, ek, False))
            rec = StructureRecord(self.tenant_id, id, rv, self.keys.data_key_version, nonce, ct, False)
            seq = self.store.put_structure(rec, {k: exact_key_hmac(self.keys.hmac_key, k, v) for k, v in ek.items()})
            self.index.add_batch([(id, canonical, rv)], self.num_threads)
            self._advance(seq)
        return seq

    def delete(self, id: UUID) -> int:
        with self._lock:
            existing = self.store.get_structure(self.tenant_id, id)
            if existing is None:
                raise NotFound(str(id))
            rv = existing.row_version + 1
            nonce, ct = self._seal(id, rv, Payload("", {}, True))
            rec = StructureRecord(self.tenant_id, id, rv, self.keys.data_key_version, nonce, ct, True)
            seq = self.store.put_structure(rec, {})
            self.index.mark_dead(id, rv)
            self._advance(seq)
        return seq

    def _advance(self, seq: int) -> None:
        """After our own write: if nothing else landed in between, remember seq; otherwise catch up."""
        if seq == self.index.last_seq + 1:
            self.index.last_seq = seq
        else:
            self._refresh_locked()

    # ----- reads -----

    def get(self, id: UUID) -> Payload | None:
        rec = self.store.get_structure(self.tenant_id, id)
        if rec is None:
            return None
        p = self._open(rec)
        return None if p.deleted else p

    def find_exact(self, kind: str, value: str) -> list[UUID]:
        h = exact_key_hmac(self.keys.hmac_key, kind, value)
        out = []
        for id_ in self.store.find_by_key(self.tenant_id, kind, h):
            p = self.get(id_)
            if p is not None and p.keys.get(kind) == value:   # the index row is a hint; the payload decides
                out.append(id_)
        return out

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None) -> list[UUID]:
        with self._lock:
            self._sync_locked()
            return self.index.substructure(query, kind, max_results, self.num_threads)

    def count_substructure(self, query: str, kind: QueryKind = "smiles") -> int:
        with self._lock:
            self._sync_locked()
            return self.index.count_substructure(query, kind, self.num_threads)

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None) -> list[tuple[UUID, float]]:
        with self._lock:
            self._sync_locked()
            return self.index.similar(query_smiles, threshold, top_k)

    def __len__(self) -> int:
        return len(self.index)

    # ----- catching up -----

    def refresh(self) -> int:
        with self._lock:
            return self._refresh_locked()

    def rebuild(self) -> None:
        with self._lock:
            self._rebuild_locked()

    def _sync_locked(self) -> None:
        cursor = self.store.seq_cursor(self.tenant_id)
        if cursor < self.index.last_seq:
            self._rebuild_locked()
        elif cursor > self.index.last_seq:
            self._refresh_locked()

    def _catch_up(self, index: StructureIndex) -> int:
        applied = 0
        while True:
            recs = self.store.changes_since(self.tenant_id, index.last_seq, self.refresh_batch)
            if not recs:
                return applied
            items = []
            for rec in recs:
                p = self._open(rec)
                if p.deleted:
                    index.mark_dead(rec.id, rec.row_version)
                else:
                    items.append((rec.id, p.smiles, rec.row_version))
            index.add_batch(items, self.num_threads)
            index.last_seq = recs[-1].seq
            applied += len(recs)

    def _refresh_locked(self) -> int:
        applied = self._catch_up(self.index)
        if self.store.live_count(self.tenant_id) != len(self.index):
            self._rebuild_locked()
        return applied

    def _rebuild_locked(self) -> None:
        fresh = StructureIndex()          # built aside, then swapped in
        self._catch_up(fresh)
        self.index = fresh
