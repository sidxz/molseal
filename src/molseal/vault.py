"""The per-tenant facade. The only module that holds keys, store and engine together.

Consistency model (single process): a put or delete updates the index in the same call, so a
search right after sees it. Every search first reads the tenant's cursor; if it moved, the vault
pulls the new rows first. If it moved backwards, or the live counts disagree, the index is rebuilt.

Multi-writer safety: put and delete compare-and-set on row_version inside the store transaction
and retry the whole read-seal-write cycle on conflict, so two writers never reuse a version.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from itertools import batched
from pathlib import Path
from uuid import UUID

from . import snapshot
from .crypto import open_sealed, pad, row_aad, seal, unpad
from .engine import QueryKind, StructureIndex, canonicalize
from .keys import KeyProvider, TenantKeys, exact_key_hmac, new_tenant_secret, unwrap_key, wrap_key
from .payload import Payload, decode, encode
from .store import StructureRecord, StructureStore, VersionConflict, WrappedKey

WRITE_ATTEMPTS = 5


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
            if self.store.max_seq(self.tenant_id) != 0:
                raise VaultError(f"tenant {self.tenant_id} has structures but no key rows; refusing to mint new keys")
            data, hm = new_tenant_secret(), new_tenant_secret()
            nd, wd = wrap_key(self._kek, self.tenant_id, "data", dv, data)
            nh, wh = wrap_key(self._kek, self.tenant_id, "hmac", hv, hm)
            self.store.put_tenant_keys(self.tenant_id, [WrappedKey("data", dv, nd, wd), WrappedKey("hmac", hv, nh, wh)])
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
        hmacs = {k: exact_key_hmac(self.keys.hmac_key, k, v) for k, v in ek.items()}
        with self._lock:
            seq, rv = self._write(id, Payload(canonical, ek, False), hmacs)
            self.index.add_batch([(id, canonical, rv)], self.num_threads)
            self._advance(seq)
        return seq

    def delete(self, id: UUID) -> int:
        with self._lock:
            seq, rv = self._write(id, Payload("", {}, True), {})
            self.index.mark_dead(id, rv)
            self._advance(seq)
        return seq

    def put_many(self, items: Iterable[tuple[UUID, str, dict[str, str] | None]], chunk: int = 10_000) -> int:
        """Bulk ingest. Each chunk is canonicalized and encrypted in Python, written in one store
        transaction (consecutive, commit-ordered sequence numbers), and appended to the index once.
        Raises before any write if a chunk holds a duplicate id or an unparseable SMILES.

        Validation and atomicity are per chunk, not per call: an id repeated in two different chunks is
        written twice (last wins), and if chunk k fails, chunks 1..k-1 stay committed and indexed. The
        exception does not carry how many rows were written; callers that resume (bench/load_molseal.py)
        pass one chunk per call and record progress after each."""
        total = 0
        for group in batched(items, chunk):
            total += self._put_chunk(list(group))
        return total

    def _put_chunk(self, group: list[tuple[UUID, str, dict[str, str] | None]]) -> int:
        ids = [i for i, _, _ in group]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in chunk")
        prepared = [(i, canonicalize(s), dict(k or {})) for i, s, k in group]   # StructureError before any write
        with self._lock:
            for _ in range(WRITE_ATTEMPTS):
                existing = self.store.get_structures(self.tenant_id, ids)
                batch, items = [], []
                for i, canonical, ek in prepared:
                    ex = existing.get(i)
                    expected = None if ex is None else ex.row_version
                    rv = 1 if expected is None else expected + 1
                    nonce, ct = self._seal(i, rv, Payload(canonical, ek, False))
                    rec = StructureRecord(self.tenant_id, i, rv, self.keys.data_key_version, nonce, ct, False)
                    batch.append((rec, {k: exact_key_hmac(self.keys.hmac_key, k, v) for k, v in ek.items()}, expected))
                    items.append((i, canonical, rv))
                try:
                    seqs = self.store.put_structures(batch)
                    break
                except VersionConflict:
                    continue
            else:
                raise VaultError(f"chunk of {len(group)}: row_version conflict persisted after {WRITE_ATTEMPTS} attempts")
            self.index.add_batch(items, self.num_threads)
            self._advance(seqs[0], seqs[-1])
        return len(batch)

    def _write(self, id: UUID, payload: Payload, hmacs: dict[str, bytes]) -> tuple[int, int]:
        """Read the row version, seal at the next one, compare-and-set. Retries on conflict."""
        for _ in range(WRITE_ATTEMPTS):
            existing = self.store.get_structure(self.tenant_id, id)
            if existing is None and payload.deleted:
                raise NotFound(str(id))
            expected = None if existing is None else existing.row_version
            rv = 1 if expected is None else expected + 1
            nonce, ct = self._seal(id, rv, payload)
            rec = StructureRecord(self.tenant_id, id, rv, self.keys.data_key_version, nonce, ct, payload.deleted)
            try:
                return self.store.put_structure(rec, hmacs, expected), rv
            except VersionConflict:
                continue
        raise VaultError(f"{id}: row_version conflict persisted after {WRITE_ATTEMPTS} attempts")

    def _advance(self, first_seq: int, last_seq: int | None = None) -> None:
        """After our own write(s): if nothing else landed in between, remember the last seq; otherwise catch up."""
        last_seq = first_seq if last_seq is None else last_seq
        if first_seq == self.index.last_seq + 1:
            self.index.last_seq = last_seq
        else:
            self._refresh_locked(*self.store.tenant_state(self.tenant_id))

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
            return self._refresh_locked(*self.store.tenant_state(self.tenant_id))

    def rebuild(self) -> None:
        with self._lock:
            self._rebuild_locked(self.store.tenant_state(self.tenant_id)[0])

    def _sync_locked(self) -> None:
        cursor, live = self.store.tenant_state(self.tenant_id)
        if cursor < self.index.last_seq:
            self._rebuild_locked(cursor)
        elif cursor > self.index.last_seq:
            self._refresh_locked(cursor, live)

    def _catch_up(self, index: StructureIndex, upto: int) -> int:
        applied = 0
        while True:
            recs = self.store.changes_since(self.tenant_id, index.last_seq, self.refresh_batch, upto)
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

    def _refresh_locked(self, cursor: int, live: int) -> int:
        """Catch up to exactly `cursor`, then compare with `live`, the count read with it."""
        applied = self._catch_up(self.index, cursor)
        if live != len(self.index) + len(self.index.unindexed):
            self._rebuild_locked(cursor)
        return applied

    def _rebuild_locked(self, upto: int) -> None:
        fresh = StructureIndex()          # built aside, then swapped in
        self._catch_up(fresh, upto)
        self.index = fresh
