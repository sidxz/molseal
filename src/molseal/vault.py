"""The per-tenant facade. The only module that holds keys, store and engine together.

Consistency model (single process): a put or delete updates the index in the same call, so a
search right after sees it. Every search first reads the tenant's cursor; if it moved, the vault
pulls the new rows first. If it moved backwards, or the live counts disagree, the index is rebuilt.

Multi-writer safety: put and delete compare-and-set on row_version inside the store transaction
and retry the whole read-seal-write cycle on conflict, so two writers never reuse a version.

Key rotation (rotate_tenant_keys) runs while vaults stay open. A vault holds every key version it has seen
and loads a new one when a row, a refused write, or the closing check of find_exact and save_snapshot names
a version it lacks. Under the tenant lock the store refuses writes sealed under an old version, so no row is
ever written under a key the rotation is about to delete.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Iterable, Sequence
from dataclasses import replace
from itertools import batched
from pathlib import Path
from uuid import UUID

import numpy as np

from . import snapshot
from .crypto import SealError, open_sealed, pad, row_aad, seal, unpad
from .engine import DEFAULT_FAMILIES, MORGAN2, FingerprintSpec, QueryKind, StructureError, StructureIndex, canonicalize
from .errors import MolsealError
from .keys import (KeyProvider, KeyProviderError, TenantKeys, exact_key_hmac, new_tenant_secret,
                   unwrap_tenant_key, wrap_tenant_key)
from .metrics import Metric, tanimoto
from .payload import Payload, PayloadError, decode, encode
from .store import KeyVersionChanged, StructureRecord, StructureStore, VersionConflict, WrappedKey

log = logging.getLogger(__name__)

WRITE_ATTEMPTS = 5


def _canonical(id: UUID, smiles: str) -> str:
    """canonicalize, naming the record in the error, since the message cannot name the structure."""
    try:
        return canonicalize(smiles)
    except StructureError as exc:
        raise StructureError(f"{id}: {exc}") from exc


def _top_versions(rows: Iterable[WrappedKey]) -> dict[str, int]:
    """The highest version of each kind. These are the current ones: a rotation inserts the next version and
    makes it current in one transaction, and deletes older ones only at the end."""
    top: dict[str, int] = {}
    for k in rows:
        top[k.kind] = max(top.get(k.kind, 0), k.version)
    return top


def _unwrap_keyring(provider: KeyProvider, tenant_id: UUID, rows: list[WrappedKey],
                    held: TenantKeys | None = None) -> TenantKeys:
    """Every key row unwrapped. Versions `held` already has are reused, so a reload asks the provider only
    for new ones, and kept even when their rows are gone."""
    rings = {"data": dict(held.data) if held else {}, "hmac": dict(held.hmac) if held else {}}
    for k in rows:
        if k.version not in rings[k.kind]:
            rings[k.kind][k.version] = unwrap_tenant_key(provider, tenant_id, k.kind, k.version, k.wrapped)
    return TenantKeys(rings["data"], rings["hmac"])


def _mint(provider: KeyProvider, tenant_id: UUID, kind: str, version: int) -> tuple[bytes, WrappedKey]:
    """A new secret and its key row, checked to unwrap to the same bytes before anything is written."""
    secret = new_tenant_secret()
    blob = wrap_tenant_key(provider, tenant_id, kind, version, secret)
    if unwrap_tenant_key(provider, tenant_id, kind, version, blob) != secret:
        raise KeyProviderError(f"key provider does not round-trip the new {kind} key; nothing was written")
    return secret, WrappedKey(kind, version, blob)


def _row_plaintext(tenant_id: UUID, key: bytes, rec: StructureRecord) -> bytes:
    """The padded plaintext of a row. SealError if it does not open under `key` and its label."""
    try:
        return open_sealed(key, rec.nonce, rec.ciphertext,
                           row_aad(tenant_id, rec.id, rec.data_key_version, rec.row_version))
    except (ValueError, struct.error) as exc:    # a nonce of the wrong length or a version beyond 32 bits: an edited row
        raise SealError("ciphertext did not open") from exc


class VaultError(MolsealError):
    """The vault cannot proceed: missing key rows, a persistent write conflict, no snapshot_dir, or a row a
    key rotation cannot open."""


class NotFound(VaultError):
    """delete() was asked to remove an id the tenant never held."""


class Vault:
    """One tenant's encrypted structures. Writes go to the store and the in-memory index together, and
    every search first catches the index up with the store's change sequence.

    Construction does work: it creates the tenant row if needed, mints and stores the tenant's keys on
    first use, loads the snapshot under `snapshot_dir` when one exists, and pulls any rows the index
    has not seen. `families` fixes which fingerprint families the index carries; `num_threads` goes to
    RDKit (-1: all cores); `refresh_batch` is how many change rows a catch-up reads per query."""

    def __init__(self, store: StructureStore, key_provider: KeyProvider, tenant_id: UUID,
                 snapshot_dir: Path | None = None, num_threads: int = -1, refresh_batch: int = 10000,
                 families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> None:
        self.store = store
        self.tenant_id = tenant_id
        self.snapshot_dir = None if snapshot_dir is None else Path(snapshot_dir)
        self.num_threads = num_threads
        self.refresh_batch = refresh_batch
        self.families = tuple(families)
        self._lock = threading.Lock()   # ponytail: one lock for reads and writes; RW lock when readers contend
        self._key_provider = key_provider
        self.tenant = store.ensure_tenant(tenant_id)
        self._keys = self._load_or_create_keys()
        self._index = self._load_snapshot() or StructureIndex(self.families)
        self.refresh()

    # ----- keys -----

    def _load_or_create_keys(self) -> TenantKeys:
        rows = self.store.get_tenant_keys(self.tenant_id)
        if not rows:
            if self.store.max_seq(self.tenant_id) != 0:
                raise VaultError(f"tenant {self.tenant_id} has structures but no key rows; refusing to mint new keys")
            dv, hv = self.tenant.data_key_version, self.tenant.hmac_key_version
            (data, wd), (hm, wh) = (_mint(self._key_provider, self.tenant_id, "data", dv),
                                    _mint(self._key_provider, self.tenant_id, "hmac", hv))
            self.store.put_tenant_keys(self.tenant_id, [wd, wh])
            return TenantKeys({dv: data}, {hv: hm})
        top = _top_versions(rows)
        # the tenant row was read first and versions only grow, so rows below it are missing, not stale
        if top.get("data", 0) < self.tenant.data_key_version or top.get("hmac", 0) < self.tenant.hmac_key_version:
            raise VaultError("tenant key rows are missing for the current key versions")
        return _unwrap_keyring(self._key_provider, self.tenant_id, rows)

    def _reload_keys(self, rows: list[WrappedKey] | None = None) -> TenantKeys:
        """Unwrap the key versions this vault does not hold yet and swap the keyring in one assignment, so a
        reader outside the lock sees the old keyring or the new one, never a mix."""
        # ponytail: no reload lock; two threads may reload at once, both build a valid keyring and the last
        # assignment wins. Add a lock if provider calls get expensive.
        if rows is None:
            rows = self.store.get_tenant_keys(self.tenant_id)
        self._keys = _unwrap_keyring(self._key_provider, self.tenant_id, rows, self._keys)
        return self._keys

    def _current(self, k: TenantKeys) -> TenantKeys:
        """`k` if it holds the tenant's current versions, read now from the key rows; else the reloaded keyring."""
        rows = self.store.get_tenant_keys(self.tenant_id)
        top = _top_versions(rows)
        if top.get("data") in k.data and top.get("hmac") in k.hmac:
            return k
        return self._reload_keys(rows)

    # ----- snapshot -----

    def _snapshot_path(self) -> Path | None:
        return None if self.snapshot_dir is None else self.snapshot_dir / f"{self.tenant_id}.molseal"

    def _load_snapshot(self) -> StructureIndex | None:
        p = self._snapshot_path()
        if p is None or not p.exists():
            return None
        try:
            return snapshot.load(p, self._keys.data, self.tenant_id, self.families)
        except snapshot.SnapshotFormatError as exc:
            log.warning("ignoring snapshot %s: %s", p, exc)
            return None          # an older format: rebuild from the store, the next save overwrites it

    def save_snapshot(self) -> Path:
        """Write the index to `<snapshot_dir>/<tenant_id>.molseal`, encrypted under the data key, so the
        next Vault for this tenant opens without replaying the store. Returns the path."""
        p = self._snapshot_path()
        if p is None:
            raise VaultError("no snapshot_dir configured")
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            k = self._current(self._keys)      # a vault that only searched may not have seen a rotation
            snapshot.save(self._index, p, k.data_key, self.tenant_id, k.data_key_version)
        return p

    # ----- row crypto -----

    def _open(self, rec: StructureRecord) -> Payload:
        v = rec.data_key_version
        key = self._keys.data.get(v) or self._reload_keys().data.get(v)
        if key is None:
            raise VaultError(f"{rec.id}: sealed under data key version {v}, which has no key row")
        return decode(unpad(_row_plaintext(self.tenant_id, key, rec)))

    def _seal(self, k: TenantKeys, id: UUID, row_version: int, payload: Payload) -> tuple[bytes, bytes]:
        aad = row_aad(self.tenant_id, id, k.data_key_version, row_version)
        return seal(k.data_key, pad(encode(payload)), aad)

    # ----- writes -----

    def put(self, id: UUID, smiles: str, keys: dict[str, str] | None = None) -> int:
        """Store or replace one structure. `smiles` is canonicalized first, so a StructureError leaves
        nothing written. `keys` are the caller's exact lookup keys, kind -> value, for find_exact.
        Returns the row's new version: 1 for a new id, one more than before for a replaced one."""
        canonical = _canonical(id, smiles)
        with self._lock:
            seq, rv = self._write(id, Payload(canonical, dict(keys or {}), False))
            self._index.add_batch([(id, canonical, rv)], self.num_threads)
            self._advance(seq)
        return rv

    def delete(self, id: UUID) -> int:
        """Tombstone one structure; NotFound if the tenant never held `id`. Returns the row's new version."""
        with self._lock:
            seq, rv = self._write(id, Payload("", {}, True))
            self._index.mark_dead(id, rv)
            self._advance(seq)
        return rv

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
        prepared = [(i, _canonical(i, s), dict(k or {})) for i, s, k in group]   # StructureError before any write
        with self._lock:
            for _ in range(WRITE_ATTEMPTS):
                k = self._keys          # once per attempt: label, key, version and HMACs from one keyring
                existing = self.store.get_structures(self.tenant_id, ids)
                batch, items = [], []
                for i, canonical, ek in prepared:
                    ex = existing.get(i)
                    expected = None if ex is None else ex.row_version
                    rv = 1 if expected is None else expected + 1
                    nonce, ct = self._seal(k, i, rv, Payload(canonical, ek, False))
                    rec = StructureRecord(self.tenant_id, i, rv, k.data_key_version, nonce, ct, False)
                    batch.append((rec, {kind: exact_key_hmac(k.hmac_key, kind, v) for kind, v in ek.items()}, expected))
                    items.append((i, canonical, rv))
                try:
                    seqs = self.store.put_structures(batch)
                    break
                except KeyVersionChanged:
                    self._reload_keys()
                except VersionConflict:
                    continue
            else:
                raise VaultError(f"chunk of {len(group)}: write conflict persisted after {WRITE_ATTEMPTS} attempts")
            self._index.add_batch(items, self.num_threads)
            self._advance(seqs[0], seqs[-1])
        return len(batch)

    def _write(self, id: UUID, payload: Payload) -> tuple[int, int]:
        """Read the row version, seal at the next one, compare-and-set. Retries on conflict, reloading the keys
        first when a key rotation moved the version."""
        for _ in range(WRITE_ATTEMPTS):
            k = self._keys              # once per attempt: label, key, version and HMACs from one keyring
            existing = self.store.get_structure(self.tenant_id, id)
            if existing is None and payload.deleted:
                raise NotFound(str(id))
            expected = None if existing is None else existing.row_version
            rv = 1 if expected is None else expected + 1
            nonce, ct = self._seal(k, id, rv, payload)
            rec = StructureRecord(self.tenant_id, id, rv, k.data_key_version, nonce, ct, payload.deleted)
            hmacs = {kind: exact_key_hmac(k.hmac_key, kind, v) for kind, v in payload.keys.items()}
            try:
                return self.store.put_structure(rec, hmacs, expected), rv
            except KeyVersionChanged:
                self._reload_keys()
            except VersionConflict:
                continue
        raise VaultError(f"{id}: write conflict persisted after {WRITE_ATTEMPTS} attempts")

    def _advance(self, first_seq: int, last_seq: int | None = None) -> None:
        """After our own write(s): if nothing else landed in between, remember the last seq; otherwise catch up."""
        last_seq = first_seq if last_seq is None else last_seq
        if first_seq == self._index.last_seq + 1:
            self._index.last_seq = last_seq
        else:
            self._refresh_locked(*self.store.tenant_state(self.tenant_id))

    # ----- reads -----

    def get(self, id: UUID) -> Payload | None:
        """The decrypted payload of a live row, or None for an unknown or deleted id."""
        rec = self.store.get_structure(self.tenant_id, id)
        if rec is None:
            return None
        p = self._open(rec)
        return None if p.deleted else p

    def find_exact(self, kind: str, value: str) -> list[UUID]:
        """Ids of live rows whose exact key `kind` equals `value`. The database holds only a keyed hash
        of each key, so every candidate is decrypted and checked before it is returned."""
        k = self._keys
        for _ in range(WRITE_ATTEMPTS):
            found: dict[UUID, None] = {}
            # Lowest version first: a rotation only moves rows up, so a row that moves between two queries is
            # found by both (deduplicated), never by neither. The closing check then proves no row moved to a
            # version this round did not query.
            for v in sorted(k.hmac):
                found.update(dict.fromkeys(self.store.find_by_key(self.tenant_id, kind, exact_key_hmac(k.hmac[v], kind, value))))
            fresh = self._current(k)
            if fresh is k:
                break
            k = fresh
        else:
            raise VaultError(f"tenant {self.tenant_id}: key versions kept changing during find_exact")
        out = []
        for id_ in found:
            p = self.get(id_)
            if p is not None and p.keys.get(kind) == value:   # the index row is a hint; the payload decides
                out.append(id_)
        return out

    def get_many(self, ids: Iterable[UUID]) -> dict[UUID, Payload]:
        """Decrypted payloads of the live rows among `ids`; deleted and unknown ids are absent."""
        out: dict[UUID, Payload] = {}
        for group in batched(ids, 10_000):
            for id_, rec in self.store.get_structures(self.tenant_id, list(group)).items():
                p = self._open(rec)
                if not p.deleted:
                    out[id_] = p
        return out

    def fingerprints(self, ids: Iterable[UUID], family: str = MORGAN2.name) -> tuple[list[UUID], np.ndarray]:
        """Packed fingerprint rows of `family` for the live ids among `ids`, in the order given, after
        the same catch-up a search does."""
        ids = list(ids)
        with self._lock:
            self._sync_locked()
            return self._index.fingerprints(ids, family)

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None, *,
                     generalized: bool = False, use_chirality: bool = False) -> list[UUID]:
        """Ids of live structures containing `query`, written in the language `kind` names. `generalized`
        matches tautomers and enumerated forms; `use_chirality` makes the query's stereo mandatory;
        `max_results` caps the hits. The index is caught up with the store first."""
        with self._lock:
            self._sync_locked()
            return self._index.substructure(query, kind, max_results, self.num_threads,
                                           generalized=generalized, use_chirality=use_chirality)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", *,
                           generalized: bool = False, use_chirality: bool = False) -> int:
        """How many live structures contain `query`; same options as substructure."""
        with self._lock:
            self._sync_locked()
            return self._index.count_substructure(query, kind, self.num_threads,
                                                 generalized=generalized, use_chirality=use_chirality)

    def similar(self, query: str, threshold: float = 0.7, top_k: int | None = None, *,
                family: str = MORGAN2.name, metric: Metric = tanimoto) -> list[tuple[UUID, float]]:
        """(id, score) pairs for live structures whose `family` fingerprint scores at least `threshold`
        against the SMILES `query` under `metric`, best first; `top_k` keeps the k best."""
        with self._lock:
            self._sync_locked()
            return self._index.similar(query, threshold, top_k, family=family, metric=metric)

    def __len__(self) -> int:
        return len(self._index)

    # ----- catching up -----

    def refresh(self) -> int:
        """Apply every store change the index has not seen. Returns how many rows were applied."""
        with self._lock:
            return self._refresh_locked(*self.store.tenant_state(self.tenant_id))

    def rebuild(self) -> None:
        """Discard the index and rebuild it from the store's full change history."""
        with self._lock:
            self._rebuild_locked(self.store.tenant_state(self.tenant_id)[0])

    def _sync_locked(self) -> None:
        cursor, live = self.store.tenant_state(self.tenant_id)
        if cursor < self._index.last_seq:
            self._rebuild_locked(cursor)
        elif cursor > self._index.last_seq:
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
        applied = self._catch_up(self._index, cursor)
        if live != len(self._index) + len(self._index.unindexed):
            self._rebuild_locked(cursor)
        return applied

    def _rebuild_locked(self, upto: int) -> None:
        fresh = StructureIndex(self.families)          # built aside, then swapped in
        self._catch_up(fresh, upto)
        self._index = fresh


def rewrap_tenant(store: StructureStore, tenant_id: UUID, old: KeyProvider, new: KeyProvider) -> int:
    """Re-wrap every key row of the tenant under `new` (KEK rotation, or moving to a key service).

    Each new blob is unwrapped and compared with the secret before anything is written, and the rows
    are replaced in one transaction, so a misconfigured `new` changes nothing. Vaults that are already
    open keep working: they hold the unwrapped tenant keys. Returns the number of rows rewritten.
    Raises VersionConflict, changing nothing, if a key rotation started or finished meanwhile; run it
    again."""
    rows = store.get_tenant_keys(tenant_id)
    if not rows:
        raise VaultError(f"tenant {tenant_id} has no key rows")
    out = []
    for k in rows:
        secret = unwrap_tenant_key(old, tenant_id, k.kind, k.version, k.wrapped)
        blob = wrap_tenant_key(new, tenant_id, k.kind, k.version, secret)
        if unwrap_tenant_key(new, tenant_id, k.kind, k.version, blob) != secret:
            raise KeyProviderError(f"new provider does not round-trip {k.kind} v{k.version}")
        out.append(WrappedKey(k.kind, k.version, blob))
    store.replace_tenant_keys(tenant_id, out)
    return len(out)


def _pending(rows: list[WrappedKey]) -> bool:
    """A key rotation started and has not finished: some kind has two versions."""
    kinds = [k.kind for k in rows]
    return len(kinds) != len(set(kinds))


def _resealed(tenant_id: UUID, keys: TenantKeys, rec: StructureRecord) -> tuple[StructureRecord, dict[str, bytes]]:
    """`rec` sealed again under the current keys at the same row version, with its exact-key hashes. The padded
    plaintext is resealed as is; decoding it only validates the row and yields its exact keys."""
    key = keys.data.get(rec.data_key_version)
    if key is None:
        raise VaultError(f"{rec.id}: sealed under data key version {rec.data_key_version}, which has no key row")
    try:
        plaintext = _row_plaintext(tenant_id, key, rec)
        payload = decode(unpad(plaintext))
    except (SealError, PayloadError) as exc:
        raise VaultError(f"{rec.id}: does not open under data key version {rec.data_key_version}; "
                         "the rotation stays pending") from exc
    dv = keys.data_key_version
    nonce, ct = seal(keys.data_key, plaintext, row_aad(tenant_id, rec.id, dv, rec.row_version))
    hmacs = {kind: exact_key_hmac(keys.hmac_key, kind, v) for kind, v in payload.keys.items()}
    return replace(rec, data_key_version=dv, nonce=nonce, ciphertext=ct), hmacs


def _reseal_pass(store: StructureStore, tenant_id: UUID, keys: TenantKeys, upto: int, batch: int) -> int:
    """Re-encrypt every row with seq <= `upto` that is still under an older data key version. Rows written after
    `upto` are under the current version already: the store refuses anything else."""
    n = last = 0
    while recs := store.changes_since(tenant_id, last, batch, upto):
        out = [_resealed(tenant_id, keys, r) for r in recs if r.data_key_version < keys.data_key_version]
        n += store.reseal_structures(out)   # KeyVersionChanged: another caller finished this rotation, began the next
        last = recs[-1].seq
        log.debug("tenant %s: %d rows re-encrypted through seq %d", tenant_id, n, last)
    return n


def rotate_tenant_keys(store: StructureStore, tenant_id: UUID, provider: KeyProvider, *,
                       resume_only: bool = False, batch: int = 1000) -> int:
    """Give the tenant new data and HMAC keys and re-encrypt every row under them, so keys taken from a
    compromised process stop opening the database. Vaults keep reading and writing meanwhile, in this process
    and others, and pick up the new keys on their own; restart them afterwards so no process memory keeps the
    old keys.

    Rows are re-encrypted in place, `batch` per transaction, so after a crash a second call finishes the job.
    With `resume_only` a call only finishes a pending rotation and never starts one: the call for a worker's
    startup. Otherwise a call after a finished rotation starts the next. Returns how many rows this call
    re-encrypted.

    Every process that opens a vault for the tenant must run a molseal release with key rotation first: an
    older one keeps writing rows under the old key after the rotation deletes it.

    Raises KeyProviderError, writing nothing, if `provider` cannot unwrap the tenant's keys; VaultError if a
    row does not open (the rotation stays pending, old keys included, until the row is fixed or deleted);
    KeyVersionChanged, a VersionConflict, if another caller finished this rotation and started the next one
    meanwhile: call again."""
    if batch < 1:
        raise ValueError("batch must be at least 1")
    rows = store.get_tenant_keys(tenant_id)
    if {k.kind for k in rows} != {"data", "hmac"}:
        raise VaultError(f"tenant {tenant_id} has no key rows")
    pending = _pending(rows)
    if resume_only and not pending:
        return 0
    # before anything is written: new keys wrapped under a provider that is not the tenant's would lock it out
    keys = _unwrap_keyring(provider, tenant_id, rows)
    if pending:
        log.info("tenant %s: resuming key rotation", tenant_id)
    else:
        top = _top_versions(rows)
        new = [_mint(provider, tenant_id, kind, top[kind] + 1)[1] for kind in ("data", "hmac")]
        if store.start_key_rotation(tenant_id, new):        # False: another caller got there first; join it
            log.info("tenant %s: key rotation to data v%d, hmac v%d started", tenant_id, top["data"] + 1, top["hmac"] + 1)
        keys = _unwrap_keyring(provider, tenant_id, store.get_tenant_keys(tenant_id), keys)
    upto = store.seq_cursor(tenant_id)
    n = 0
    for _ in range(3):      # one pass normally (see _reseal_pass); the bound guards against a bug looping forever
        n += _reseal_pass(store, tenant_id, keys, upto, batch)
        if store.finish_key_rotation(tenant_id):
            log.info("tenant %s: key rotation to data v%d finished, %d rows re-encrypted", tenant_id,
                     keys.data_key_version, n)
            return n
    raise VaultError(f"tenant {tenant_id}: rows under an old data key version remain; call again")
