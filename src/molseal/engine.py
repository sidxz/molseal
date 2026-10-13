"""In-memory structure index: RDKit SubstructLibrary for substructure, a packed Morgan
matrix for similarity, and a live mask so edits and deletes work on an append-only library.

No SQL lives here. Positions are integers; the vault maps ids to its own tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal
from uuid import UUID

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.rdSubstructLibrary import AddPatterns, CachedTrustedSmilesMolHolder, SubstructLibrary

RDLogger.DisableLog("rdApp.*")

FP_BITS = 2048
FP_BYTES = FP_BITS // 8
MORGAN_RADIUS = 2
QueryKind = Literal["smiles", "smarts"]

_MORGAN = rdFingerprintGenerator.GetMorganGenerator(radius=MORGAN_RADIUS, fpSize=FP_BITS)


class StructureError(ValueError):
    """A SMILES or query could not be parsed."""


def canonicalize(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise StructureError(f"cannot parse SMILES: {smiles!r}")
    out = Chem.MolToSmiles(mol)
    if Chem.MolFromSmiles(out) is None:     # the index re-parses what we store, so it must parse
        raise StructureError(f"canonical SMILES does not re-parse: {smiles!r}")
    return out


def parse_many(smiles: Sequence[str]) -> list[Chem.Mol | None]:
    # ponytail: serial parse; swap for a multithreaded supplier or a process pool when ingest time matters
    return [Chem.MolFromSmiles(s) for s in smiles]


def _pack(fp) -> np.ndarray:
    bits = np.frombuffer(fp.ToBitString().encode("ascii"), dtype=np.uint8) - ord("0")
    return np.packbits(bits)


def morgan_matrix(mols: Sequence[Chem.Mol], num_threads: int = -1) -> np.ndarray:
    """Morgan radius-2, 2048-bit fingerprints packed to (n, 256) uint8."""
    if not mols:
        return np.zeros((0, FP_BYTES), dtype=np.uint8)
    fps = _MORGAN.GetFingerprints(list(mols), numThreads=num_threads)
    return np.stack([_pack(fp) for fp in fps])


def _parse_query(query: str, kind: QueryKind) -> Chem.Mol:
    q = Chem.MolFromSmarts(query) if kind == "smarts" else Chem.MolFromSmiles(query)
    if q is None:
        raise StructureError(f"cannot parse {kind} query: {query!r}")
    return q


class StructureIndex:
    def __init__(self) -> None:
        self._lib = SubstructLibrary(CachedTrustedSmilesMolHolder())
        self._patterns = False          # True once AddPatterns has run (initial build)
        self._ids: list[UUID] = []      # position -> id
        self._pos_of: dict[UUID, int] = {}   # id -> latest position (live or dead)
        self._row_versions: dict[UUID, int] = {}
        self._n = 0
        self._cap = 0
        self._live = np.zeros(0, dtype=bool)
        self._fps = np.zeros((0, FP_BYTES), dtype=np.uint8)
        self._pop = np.zeros(0, dtype=np.uint16)
        self._order_dirty = False       # the library's search order is out of date
        self._ordered = False           # a search order has been applied at least once
        self.unindexed: set[UUID] = set()   # ids whose current live version RDKit could not parse
        self.last_seq = 0

    # ----- bookkeeping -----

    def __len__(self) -> int:
        return int(self._live[: self._n].sum())

    @property
    def positions(self) -> int:
        return self._n

    def has(self, id: UUID, row_version: int) -> bool:
        return self._row_versions.get(id, 0) >= row_version

    def _ensure_capacity(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        cap = max(need, self._cap * 2, 1024)
        live = np.zeros(cap, dtype=bool); live[: self._n] = self._live[: self._n]
        fps = np.zeros((cap, FP_BYTES), dtype=np.uint8); fps[: self._n] = self._fps[: self._n]
        pop = np.zeros(cap, dtype=np.uint16); pop[: self._n] = self._pop[: self._n]
        self._live, self._fps, self._pop, self._cap = live, fps, pop, cap

    def _ensure_order(self) -> None:
        """RDKit's library cannot delete; a search order listing only live positions is the sanctioned
        workaround. AddMol does not extend an existing order, so deaths and (once an order exists)
        appends mark it dirty, and it is applied once here, before a search. Callers guard
        len(self) == 0: an empty order makes RDKit search every position."""
        if self._order_dirty:
            self._lib.SetSearchOrder(np.flatnonzero(self._live[: self._n]).tolist())
            self._order_dirty = False
            self._ordered = True

    # ----- ingest -----

    def add_batch(self, items: Sequence[tuple[UUID, str, int]], num_threads: int = -1) -> int:
        """Add (id, canonical_smiles, row_version) items. Skips rows already seen at this version
        or newer. An id seen before gets its old position marked dead. A row RDKit cannot parse is
        recorded at its version, its old position dies, and its id goes into self.unindexed."""
        fresh = [it for it in items if not self.has(it[0], it[2])]
        if not fresh:
            return 0
        mols = parse_many([s for _, s, _ in fresh])
        keep = []
        for (id_, s, rv), m in zip(fresh, mols):
            if m is None:
                self._kill(id_)
                self._row_versions[id_] = rv
                self.unindexed.add(id_)
            else:
                keep.append(((id_, s, rv), m))
        if not keep:
            return 0
        fps = morgan_matrix([m for _, m in keep], num_threads)
        self._ensure_capacity(len(keep))
        for (id_, _, rv), m in keep:
            self._kill(id_)
            self.unindexed.discard(id_)
            self._lib.AddMol(m)
            pos = self._n
            self._ids.append(id_)
            self._pos_of[id_] = pos
            self._row_versions[id_] = rv
            self._live[pos] = True
            self._n += 1
        start = self._n - len(keep)
        self._fps[start : self._n] = fps
        self._pop[start : self._n] = np.bitwise_count(fps).sum(axis=1).astype(np.uint16)
        self._order_dirty = self._order_dirty or self._ordered
        return len(keep)

    def _ensure_patterns(self, num_threads: int) -> None:
        if not self._patterns and self._n > 0:
            AddPatterns(self._lib, numThreads=num_threads)
            self._patterns = True

    # ----- substructure -----

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None,
                     num_threads: int = -1) -> list[UUID]:
        if len(self) == 0:
            return []
        q = _parse_query(query, kind)
        self._ensure_patterns(num_threads)
        self._ensure_order()
        hits = self._lib.GetMatches(q, recursionPossible=True, useChirality=False, useQueryQueryMatches=False,
                                    numThreads=num_threads, maxResults=-1 if max_results is None else int(max_results))
        out: dict[UUID, None] = {}
        for pos in hits:
            id_ = self._ids[pos]
            if self._live[pos] and self._pos_of[id_] == pos:
                out[id_] = None
        return list(out)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", num_threads: int = -1) -> int:
        if len(self) == 0:
            return 0
        q = _parse_query(query, kind)
        self._ensure_patterns(num_threads)
        self._ensure_order()
        return int(self._lib.CountMatches(q, recursionPossible=True, useChirality=False,
                                          useQueryQueryMatches=False, numThreads=num_threads))

    # ----- deletes -----

    def mark_dead(self, id: UUID, row_version: int) -> bool:
        """Record a tombstone at row_version. Returns False if this version was already applied."""
        if self.has(id, row_version):
            return False
        self._row_versions[id] = row_version
        self._kill(id)
        self.unindexed.discard(id)
        return True

    def _kill(self, id: UUID) -> None:
        pos = self._pos_of.get(id)
        if pos is not None and self._live[pos]:
            self._live[pos] = False
            self._order_dirty = True

    # ----- similarity -----

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None) -> list[tuple[UUID, float]]:
        if len(self) == 0:
            return []
        q = _parse_query(query_smiles, "smiles")
        qfp = morgan_matrix([q])[0]
        qpop = int(np.bitwise_count(qfp).sum())
        q64 = qfp.view(np.uint64)
        n = self._n
        sims = np.empty(n, dtype=np.float64)
        step = 250_000
        for s in range(0, n, step):
            e = min(n, s + step)
            rows = self._fps[s:e].view(np.uint64)
            inter = np.bitwise_count(rows & q64).sum(axis=1).astype(np.float64)
            denom = self._pop[s:e].astype(np.float64) + qpop - inter
            sims[s:e] = np.divide(inter, denom, out=np.zeros_like(inter), where=denom > 0)
        sims[~self._live[:n]] = -1.0
        idx = np.flatnonzero(sims >= threshold)
        if top_k is not None and len(idx) > top_k:
            idx = idx[np.argpartition(-sims[idx], top_k - 1)[:top_k]]
        idx = idx[np.argsort(-sims[idx], kind="stable")]
        return [(self._ids[i], float(sims[i])) for i in idx]

    # ----- serialization (the snapshot module encrypts these parts) -----

    def to_parts(self) -> dict[str, bytes]:
        n = self._n
        meta = {"n": n, "last_seq": self.last_seq, "patterns": self._patterns,
                "unindexed": sorted(str(i) for i in self.unindexed)}
        return {
            "meta": json.dumps(meta, sort_keys=True).encode("ascii"),
            "lib": bytes(self._lib.Serialize()),
            "ids": b"".join(i.bytes for i in self._ids),
            "live": self._live[:n].tobytes(),
            "rv": np.array([self._row_versions[i] for i in self._ids], dtype=np.uint32).tobytes(),
            "fps": self._fps[:n].tobytes(),
        }

    @classmethod
    def from_parts(cls, parts: dict[str, bytes]) -> "StructureIndex":
        meta = json.loads(parts["meta"])
        n = int(meta["n"])
        self = cls()
        self._lib = SubstructLibrary(parts["lib"])
        self._patterns = bool(meta["patterns"])
        self.last_seq = int(meta["last_seq"])
        self._ids = [UUID(bytes=parts["ids"][i * 16 : (i + 1) * 16]) for i in range(n)]
        live = np.frombuffer(parts["live"], dtype=bool)[:n]
        rvs = np.frombuffer(parts["rv"], dtype=np.uint32)[:n]
        fps = np.frombuffer(parts["fps"], dtype=np.uint8).reshape(n, FP_BYTES)
        self._ensure_capacity(n)
        self._live[:n] = live
        self._fps[:n] = fps
        self._pop[:n] = np.bitwise_count(fps).sum(axis=1).astype(np.uint16)
        self._n = n
        for pos, (id_, rv) in enumerate(zip(self._ids, rvs)):
            self._pos_of[id_] = pos                    # later positions win
            self._row_versions[id_] = max(int(rv), self._row_versions.get(id_, 0))
        self.unindexed = {UUID(i) for i in meta.get("unindexed", [])}
        self._order_dirty = not bool(self._live[:n].all())
        return self
