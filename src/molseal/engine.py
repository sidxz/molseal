"""In-memory structure index: RDKit SubstructLibrary for substructure, one packed fingerprint matrix
per registered family for similarity, and a live mask so edits and deletes work on an append-only
library.

No SQL lives here. Positions are integers; the vault maps ids to its own tables.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal
from uuid import UUID

import numpy as np
from pydantic import Field
from pydantic.dataclasses import dataclass
from rdkit import Chem, rdBase
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.rdGeneralizedSubstruct import CreateExtendedQueryMol, MolHasSubstructMatch
from rdkit.Chem.rdSubstructLibrary import AddPatterns, CachedTrustedSmilesMolHolder, SubstructLibrary, TautomerPatternHolder

from .errors import MolsealError
from .metrics import Metric, tanimoto

QueryKind = Literal["smiles", "smarts", "molblock"]
_PARSERS: dict[str, Callable[[str], Chem.Mol | None]] = {
    "smiles": Chem.MolFromSmiles,
    "smarts": Chem.MolFromSmarts,            # taken literally: no aromaticity perception
    "molblock": Chem.MolFromMolBlock,        # sanitized, explicit hydrogens removed
}


@dataclass(frozen=True)
class FingerprintSpec:
    """A fingerprint family. `name` is its identity: change it when the generator changes. `bits` is a
    positive multiple of 64 below 65536 (per-row bit counts are uint16). `generator` is a zero-argument
    factory returning an RDKit fingerprint generator: anything with GetFingerprints(mols, numThreads=n)
    giving `bits`-wide bit vectors. A bad field raises pydantic's ValidationError, a ValueError."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    bits: int = Field(gt=0, lt=65536, multiple_of=64)
    generator: Callable[[], Any] = Field()

    @property
    def nbytes(self) -> int:
        return self.bits // 8


def _morgan(**kw: Any) -> Callable[[], Any]:
    return lambda: rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, **kw)


MORGAN2 = FingerprintSpec("morgan2", 2048, _morgan())
MORGAN2_CHIRAL = FingerprintSpec("morgan2-chiral", 2048, _morgan(includeChirality=True))
FEATMORGAN2 = FingerprintSpec("featmorgan2", 2048, lambda: rdFingerprintGenerator.GetMorganGenerator(
    radius=2, fpSize=2048, atomInvariantsGenerator=rdFingerprintGenerator.GetMorganFeatureAtomInvGen()))
DEFAULT_FAMILIES: tuple[FingerprintSpec, ...] = (MORGAN2,)


class StructureError(MolsealError, ValueError):
    """A SMILES or query could not be parsed."""


def canonicalize(smiles: str) -> str:
    """RDKit's canonical SMILES for `smiles`: the form the vault stores and the index re-parses."""
    with rdBase.BlockLogs():        # RDKit prints parse errors; the exception below carries them instead
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            raise StructureError(f"cannot parse SMILES: {smiles!r}")
        out: str = Chem.MolToSmiles(mol)
        if Chem.MolFromSmiles(out) is None:     # the index re-parses what we store, so it must parse
            raise StructureError(f"canonical SMILES does not re-parse: {smiles!r}")
    return out


def parse_many(smiles: Sequence[str]) -> list[Chem.Mol | None]:
    """RDKit molecules for canonical SMILES, None where a string no longer parses."""
    # ponytail: serial parse; swap for a multithreaded supplier or a process pool when ingest time matters
    with rdBase.BlockLogs():
        return [Chem.MolFromSmiles(s) for s in smiles]


def _pack(fp: Any) -> np.ndarray:
    bits = np.frombuffer(fp.ToBitString().encode("ascii"), dtype=np.uint8) - ord("0")
    return np.packbits(bits)


def pack_fingerprints(generator: Any, mols: Sequence[Chem.Mol], nbytes: int, num_threads: int = -1) -> np.ndarray:
    """Fingerprints of `mols` from `generator`, packed to (n, nbytes) uint8. Bit i of a vector is
    np.unpackbits(row)[i]."""
    if not mols:
        return np.zeros((0, nbytes), dtype=np.uint8)
    fps = generator.GetFingerprints(list(mols), numThreads=num_threads)
    if fps[0].GetNumBits() != nbytes * 8:
        raise ValueError(f"generator produced {fps[0].GetNumBits()}-bit vectors, the family declares {nbytes * 8}")
    return np.stack([_pack(fp) for fp in fps])


class _Family:
    __slots__ = ("fps", "gen", "pop", "spec")

    def __init__(self, spec: FingerprintSpec, cap: int) -> None:
        self.spec, self.gen = spec, spec.generator()
        self.fps = np.zeros((cap, spec.nbytes), dtype=np.uint8)
        self.pop = np.zeros(cap, dtype=np.uint16)

    def grow(self, cap: int, n: int) -> None:
        fps = np.zeros((cap, self.spec.nbytes), dtype=np.uint8)
        fps[:n] = self.fps[:n]
        pop = np.zeros(cap, dtype=np.uint16)
        pop[:n] = self.pop[:n]
        self.fps, self.pop = fps, pop

    def fill(self, start: int, mat: np.ndarray) -> None:
        self.fps[start : start + len(mat)] = mat
        self.pop[start : start + len(mat)] = np.bitwise_count(mat).sum(axis=1).astype(np.uint16)


def _parse_query(query: str, kind: QueryKind) -> Chem.Mol:
    parser = _PARSERS.get(kind)
    if parser is None:
        raise StructureError(f"unknown query kind {kind!r}")
    try:
        with rdBase.BlockLogs():
            q = parser(query)
    except (ValueError, RuntimeError):
        q = None
    if q is None:
        raise StructureError(f"cannot parse {kind} query: {query!r}")
    return q


def _generalize(q: Chem.Mol, query: str, kind: QueryKind) -> Chem.Mol:
    try:   # positional: doEnumeration=True, doTautomers=True, adjustQueryProperties=False (the cartridge's defaults)
        with rdBase.BlockLogs():
            return CreateExtendedQueryMol(q, True, True, False)
    except (ValueError, RuntimeError) as exc:
        raise StructureError(f"cannot build a generalized query from {kind} {query!r}: {exc}") from exc


class StructureIndex:
    """Positions 0..n-1 of an append-only RDKit library plus one packed fingerprint matrix per family.
    An id maps to its latest position; older positions of the same id, and deleted ids, are dead in
    `_live` and skipped by every search. `add_batch` and `mark_dead` are idempotent per row version,
    so replaying a change log is safe. Vault owns one of these; callers normally do not touch it."""

    def __init__(self, families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> None:
        if not families:
            raise ValueError("an index needs at least one fingerprint family")
        self._lib = SubstructLibrary(CachedTrustedSmilesMolHolder())
        self._patterns = False          # True once AddPatterns has run (initial build)
        self._ids: list[UUID] = []      # position -> id
        self._pos_of: dict[UUID, int] = {}   # id -> latest position (live or dead)
        self._row_versions: dict[UUID, int] = {}
        self._n = 0
        self._cap = 0
        self._live = np.zeros(0, dtype=bool)
        self._fams: dict[str, _Family] = {}
        for spec in families:
            if spec.name in self._fams:
                raise ValueError(f"duplicate fingerprint family {spec.name!r}")
            self._fams[spec.name] = _Family(spec, 0)
        self._order_dirty = False       # the library's search order is out of date
        self._ordered = False           # a search order has been applied at least once
        self.unindexed: set[UUID] = set()   # ids whose current live version RDKit could not parse
        self.last_seq = 0

    # ----- families -----

    @property
    def families(self) -> tuple[FingerprintSpec, ...]:
        """Registered families, in registration order."""
        return tuple(f.spec for f in self._fams.values())

    def _family(self, name: str) -> _Family:
        try:
            return self._fams[name]
        except KeyError:
            raise ValueError(f"unknown fingerprint family {name!r}") from None

    def add_family(self, spec: FingerprintSpec, num_threads: int = -1) -> None:
        """Register a family on a live index and compute it for every position from the SMILES the
        library already holds (the ingest parse path, so the bits are identical). No-op when the name
        is present with the same width; ValueError when it is present with another width."""
        have = self._fams.get(spec.name)
        if have is not None:
            if have.spec.bits != spec.bits:
                raise ValueError(f"family {spec.name!r} is registered with {have.spec.bits} bits, not {spec.bits}")
            return
        fam = _Family(spec, self._cap)
        self._compute(fam, num_threads)
        self._fams[spec.name] = fam

    def drop_family(self, name: str) -> None:
        """Forget a family and free its matrix; the last one cannot go."""
        self._family(name)
        if len(self._fams) == 1:
            raise ValueError("cannot drop the last fingerprint family")
        del self._fams[name]

    def _compute(self, fam: _Family, num_threads: int) -> None:
        """Fill `fam` for positions 0..n-1 from the library's own SMILES."""
        if self._n == 0:
            return
        # ponytail: serial re-parse ceiling: one MolToSmiles plus one parse per held position; a threaded
        # supplier when late registration on millions of rows matters
        mols = parse_many([Chem.MolToSmiles(self._lib.GetMol(i)) for i in range(self._n)])
        if any(m is None for m in mols):
            raise StructureError("a held structure no longer parses")
        fam.fill(0, pack_fingerprints(fam.gen, mols, fam.spec.nbytes, num_threads))

    def fingerprints(self, ids: Iterable[UUID], family: str = MORGAN2.name) -> tuple[list[UUID], np.ndarray]:
        """Packed rows of `family` for the live, indexed ids among `ids`, in the order given."""
        fam = self._family(family)
        found, rows = [], []
        for id_ in ids:
            pos = self._pos_of.get(id_)
            if pos is not None and self._live[pos]:
                found.append(id_)
                rows.append(pos)
        if not rows:
            return [], np.zeros((0, fam.spec.nbytes), dtype=np.uint8)
        return found, fam.fps[np.array(rows, dtype=np.int64)]

    # ----- bookkeeping -----

    def __len__(self) -> int:
        return int(self._live[: self._n].sum())

    @property
    def positions(self) -> int:
        """Positions ever appended, live or dead; len(self) counts only the live ones."""
        return self._n

    def has(self, id: UUID, row_version: int) -> bool:
        """True once `id` has been applied at `row_version` or a newer one."""
        return self._row_versions.get(id, 0) >= row_version

    def _ensure_capacity(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        cap = max(need, self._cap * 2, 1024)
        live = np.zeros(cap, dtype=bool)
        live[: self._n] = self._live[: self._n]
        self._live, self._cap = live, cap
        for f in self._fams.values():
            f.grow(cap, self._n)

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
        kept_mols = [m for _, m in keep]
        mats = {name: pack_fingerprints(f.gen, kept_mols, f.spec.nbytes, num_threads) for name, f in self._fams.items()}
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
        for name, f in self._fams.items():
            f.fill(start, mats[name])
        self._order_dirty = self._order_dirty or self._ordered
        return len(keep)

    def _ensure_patterns(self, num_threads: int) -> None:
        # The tautomer holder screens plain and generalized queries alike; the plain holder would
        # skip the screen for generalized queries and scan every molecule.
        if not self._patterns and self._n > 0:
            AddPatterns(self._lib, TautomerPatternHolder(), numThreads=num_threads)
            self._patterns = True

    # ----- substructure -----

    def _ez_chiral_hits(self, plain: Chem.Mol, q: Chem.Mol, generalized: bool, num_threads: int) -> list[int]:
        """Positions matching a query with double-bond stereo under chirality. CachedTrustedSmilesMolHolder
        never assigns bond stereo to its targets, so the library cannot check E/Z: search without
        chirality (a superset), then re-check each hit on a fully parsed molecule."""
        # ponytail: one full parse per chirality-off hit; a broad E/Z query on a large library pays hits x parse
        params = Chem.SubstructMatchParameters()
        params.useChirality = True
        keep = []
        for pos in self._lib.GetMatches(q, recursionPossible=True, useChirality=False, useQueryQueryMatches=False,
                                        numThreads=num_threads, maxResults=-1):
            m = Chem.MolFromSmiles(Chem.MolToSmiles(self._lib.GetMol(pos)))
            if m is not None and (MolHasSubstructMatch(m, q, params) if generalized else m.HasSubstructMatch(plain, useChirality=True)):
                keep.append(pos)
        return keep

    def _query(self, query: str, kind: QueryKind, generalized: bool, use_chirality: bool,
               num_threads: int) -> tuple[Chem.Mol, Chem.Mol, bool]:
        plain = _parse_query(query, kind)
        q = _generalize(plain, query, kind) if generalized else plain
        self._ensure_patterns(num_threads)
        self._ensure_order()
        ez = use_chirality and any(b.GetStereo() != Chem.BondStereo.STEREONONE for b in plain.GetBonds())
        return plain, q, ez

    def _live_ids(self, hits: Iterable[int]) -> list[UUID]:
        out: dict[UUID, None] = {}
        for pos in hits:
            id_ = self._ids[pos]
            if self._live[pos] and self._pos_of[id_] == pos:
                out[id_] = None
        return list(out)

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None,
                     num_threads: int = -1, *, generalized: bool = False, use_chirality: bool = False) -> list[UUID]:
        """Ids of live structures containing `query`, in position order. `generalized` matches tautomers
        and enumerated forms; `use_chirality` makes stereo in the query mandatory in the match."""
        if len(self) == 0:
            return []
        plain, q, ez = self._query(query, kind, generalized, use_chirality, num_threads)
        if ez:
            out = self._live_ids(self._ez_chiral_hits(plain, q, generalized, num_threads))
            return out if max_results is None else out[: int(max_results)]
        hits = self._lib.GetMatches(q, recursionPossible=True, useChirality=use_chirality, useQueryQueryMatches=False,
                                    numThreads=num_threads, maxResults=-1 if max_results is None else int(max_results))
        return self._live_ids(hits)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", num_threads: int = -1, *,
                           generalized: bool = False, use_chirality: bool = False) -> int:
        """How many live structures contain `query`; same options as substructure."""
        if len(self) == 0:
            return 0
        plain, q, ez = self._query(query, kind, generalized, use_chirality, num_threads)
        if ez:
            return len(self._live_ids(self._ez_chiral_hits(plain, q, generalized, num_threads)))
        return int(self._lib.CountMatches(q, recursionPossible=True, useChirality=use_chirality,
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

    def similar(self, query: str, threshold: float = 0.7, top_k: int | None = None, *,
                family: str = MORGAN2.name, metric: Metric = tanimoto) -> list[tuple[UUID, float]]:
        """(id, score) for live structures scoring at least `threshold` against the SMILES `query` under
        `metric` on `family`, best first; `top_k` keeps the k best. One pass over the packed matrix."""
        fam = self._family(family)
        if len(self) == 0:
            return []
        q = _parse_query(query, "smiles")
        qfp = pack_fingerprints(fam.gen, [q], fam.spec.nbytes)[0]
        qpop = int(np.bitwise_count(qfp).sum())
        q64 = qfp.view(np.uint64)
        n = self._n
        sims = np.empty(n, dtype=np.float64)
        step = 250_000
        for s in range(0, n, step):
            e = min(n, s + step)
            inter = np.bitwise_count(fam.fps[s:e].view(np.uint64) & q64).sum(axis=1)
            sims[s:e] = metric(inter, qpop, fam.pop[s:e])
        idx = np.flatnonzero(self._live[:n] & (sims >= threshold))
        if top_k is not None and len(idx) > top_k:
            idx = idx[np.argpartition(-sims[idx], top_k - 1)[:top_k]]
        idx = idx[np.argsort(-sims[idx], kind="stable")]
        return [(self._ids[i], float(sims[i])) for i in idx]

    # ----- serialization (the snapshot module encrypts these parts) -----

    def to_parts(self) -> dict[str, bytes]:
        """Named byte strings that from_parts rebuilds the index from; `meta` lists the others."""
        n = self._n
        fams = [{"name": f.spec.name, "bits": f.spec.bits} for f in self._fams.values()]
        order = ["lib", "ids", "live", "rv"] + [f"fp:{f['name']}" for f in fams]
        meta = {"n": n, "last_seq": self.last_seq, "patterns": self._patterns,
                "unindexed": sorted(str(i) for i in self.unindexed), "families": fams, "parts": order}
        out = {
            "meta": json.dumps(meta, sort_keys=True).encode("ascii"),
            "lib": bytes(self._lib.Serialize()),
            "ids": b"".join(i.bytes for i in self._ids),
            "live": self._live[:n].tobytes(),
            "rv": np.array([self._row_versions[i] for i in self._ids], dtype=np.uint32).tobytes(),
        }
        for f in self._fams.values():
            out[f"fp:{f.spec.name}"] = f.fps[:n].tobytes()
        return out

    @classmethod
    def from_parts(cls, parts: dict[str, bytes], families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES,
                   num_threads: int = -1) -> StructureIndex:
        """Rebuild from parts. A requested family stored under the same name and width is taken from the
        parts; any other requested family is computed from the library's SMILES; stored families nobody
        requested are dropped."""
        meta = json.loads(parts["meta"])
        n = int(meta["n"])
        stored = {f["name"]: int(f["bits"]) for f in meta.get("families", [])}
        self = cls(families)
        self._lib = SubstructLibrary(parts["lib"])
        self._patterns = bool(meta["patterns"])
        self.last_seq = int(meta["last_seq"])
        self._ids = [UUID(bytes=parts["ids"][i * 16 : (i + 1) * 16]) for i in range(n)]
        live = np.frombuffer(parts["live"], dtype=bool)[:n]
        rvs = np.frombuffer(parts["rv"], dtype=np.uint32)[:n]
        self._ensure_capacity(n)
        self._live[:n] = live
        self._n = n
        for pos, (id_, rv) in enumerate(zip(self._ids, rvs)):
            self._pos_of[id_] = pos                    # later positions win
            self._row_versions[id_] = max(int(rv), self._row_versions.get(id_, 0))
        self.unindexed = {UUID(i) for i in meta.get("unindexed", [])}
        self._order_dirty = not bool(self._live[:n].all())
        for fam in self._fams.values():
            if stored.get(fam.spec.name) == fam.spec.bits:
                fam.fill(0, np.frombuffer(parts[f"fp:{fam.spec.name}"], dtype=np.uint8).reshape(n, fam.spec.nbytes))
            else:
                self._compute(fam, num_threads)
        return self
