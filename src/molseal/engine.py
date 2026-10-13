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
    return Chem.MolToSmiles(mol)


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
        self._order_dirty = False       # True once any position has died
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

    def _apply_order(self) -> None:
        """RDKit's library cannot delete; a search order listing only live positions is the sanctioned
        workaround. AddMol does not extend an existing order, so this runs after every append."""
        if self._order_dirty:
            self._lib.SetSearchOrder([int(p) for p in np.flatnonzero(self._live[: self._n])])

    # ----- ingest -----

    def add_batch(self, items: Sequence[tuple[UUID, str, int]], num_threads: int = -1) -> int:
        """Add (id, canonical_smiles, row_version) items. Skips rows already seen at this version
        or newer, and rows RDKit cannot parse. An id seen before gets its old position marked dead."""
        fresh = [it for it in items if not self.has(it[0], it[2])]
        if not fresh:
            return 0
        mols = parse_many([s for _, s, _ in fresh])
        keep = [(it, m) for it, m in zip(fresh, mols) if m is not None]
        if not keep:
            return 0
        fps = morgan_matrix([m for _, m in keep], num_threads)
        self._ensure_capacity(len(keep))
        for (id_, _, rv), m in keep:
            old = self._pos_of.get(id_)
            if old is not None:
                self._live[old] = False
                self._order_dirty = True
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
        self._apply_order()
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
        return int(self._lib.CountMatches(q, recursionPossible=True, useChirality=False,
                                          useQueryQueryMatches=False, numThreads=num_threads))
