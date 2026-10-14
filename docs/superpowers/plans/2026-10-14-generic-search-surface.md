# Generic Search Surface Implementation Plan (milestone 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give molseal caller-registered fingerprint families, caller-chosen similarity metrics, substructure options (chirality, tautomer-insensitive, molblock), and bulk reads of decrypted structures and fingerprints, with every behaviour proven against RDKit or the cartridge at 500k.

**Architecture:** The engine holds one packed bit matrix per registered family and runs one scan path that hands intersection counts to a metric function. Substructure gains query kinds and options on top of RDKit's extended query mol and tautomer pattern holder. The snapshot becomes format 2 with one part per family; an old file is refused with a distinct error and the vault rebuilds. The bench gains gates 5–10.

**Tech Stack:** Python 3.13, RDKit 2025.03.3, numpy ≥ 2.0, SQLAlchemy 2, pytest. Dev Postgres on :5439; bench cartridge on :5440 (`bench/bench/bench`), bench molseal on :5441.

**Spec:** `docs/superpowers/specs/2026-10-14-generic-search-surface-design.md`

**Branch:** `m3-search`, created from `main` at `b516145` (spec commit). Baseline code is tag `milestone-2`.

## Global Constraints

- The library stays generic: no consumer vocabulary, no product names, tenants opaque, exact keys caller-defined.
- Never commit `*.key`, `bench/data`, `bench/results`, `talks/`. ChEMBL is never redistributed.
- RDKit pinned at `2025.3.3` both sides; numpy ≥ 2.0 (`np.bitwise_count`); Python ≥ 3.13 (`itertools.batched`).
- Every new behaviour has a test; every behaviour with a cartridge equivalent has a parity gate.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and never carry a session trailer. Commit with explicit pathspecs (`git commit -m ... -- <paths>`).
- Tests run with `MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal` (start it with `docker compose up -d db`). The bench cartridge tests need `MOLSEAL_CARTRIDGE_URL=postgresql+psycopg://bench:bench@localhost:5440/bench`.
- Existing call sites keep working: `similar(q, 0.7)`, `similar(q, 0.0, 100)`, `substructure(q, kind, max_results, num_threads)`, `count_substructure(q, kind, num_threads)` stay positional-compatible.

## Review Focus

1. A family registered after load must be bit-identical to the same family computed at ingest, including positions that were edited or deleted since. Test in Task 2.
2. `generalized=True` on a SMARTS or molblock query must either answer or raise `StructureError`; it must never crash the interpreter. Test in Task 4.
3. A snapshot whose header is edited to claim format 2 on format-1 content must raise `SnapshotError`, not be treated as "no snapshot". Test in Task 3.
4. A vault that finds a format-1 snapshot must rebuild from the database and its next save must be format 2. Test in Task 5.
5. Tversky's orientation: alpha weights the query. Pinned against RDKit in Task 1 and against the cartridge's argument order in Task 6.

## File Structure

- Create `src/molseal/metrics.py` — the four metrics as pure functions of three counts.
- Modify `src/molseal/engine.py` — `FingerprintSpec`, shipped families, per-family matrices, `add_family`/`drop_family`/`fingerprints`, `similar(family, metric)`, query kinds and options, tautomer pattern holder, parts with one `fp:<name>` per family.
- Modify `src/molseal/snapshot.py` — format 2, parts order from meta, `SnapshotFormatError`, `load(..., families)`.
- Modify `src/molseal/vault.py` — `families` parameter, `get_many`, `fingerprints`, option pass-through, rebuild on `SnapshotFormatError`.
- Modify `bench/queries.py` — seeded chiral queries. Modify `bench/parity.py` — gates 5–10.
- Create `tests/test_metrics.py`. Modify `tests/test_engine.py`, `tests/test_snapshot.py`, `tests/test_vault.py`, `tests/bench/test_queries.py`, `tests/bench/test_parity.py`.
- Create `docs/examples/registry-adapter.md`, `docs/backlog/2026-10-14-similarity-band-index.md`, `docs/results/2026-10-14-500k-search-surface/`. Modify `README.md`, `bench/README.md`, `docs/results/README.md`, `docs/backlog/2026-10-13-generic-search-surface.md`.

---

### Task 1: Metrics module

**Files:**
- Create: `src/molseal/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: `Metric = Callable[[np.ndarray, int, np.ndarray], np.ndarray]`; `tanimoto(c, a, b)`, `dice(c, a, b)`, `cosine(c, a, b)`, `tversky(query_weight, target_weight) -> Metric`. `c` = per-row intersection counts, `a` = query bit count, `b` = per-row bit counts; result float64; zero denominator → 0.0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_metrics.py
import numpy as np
import pytest
from rdkit import DataStructs

from molseal import metrics


def _bv(bits: int, rng: np.random.Generator) -> DataStructs.ExplicitBitVect:
    bv = DataStructs.ExplicitBitVect(2048)
    for i in rng.choice(2048, size=bits, replace=False):
        bv.SetBit(int(i))
    return bv


@pytest.fixture(scope="module")
def pairs():
    rng = np.random.default_rng(7)
    q = _bv(40, rng)
    targets = [_bv(int(n), rng) for n in rng.integers(5, 120, size=50)]
    qbits = set(q.GetOnBits())
    c = np.array([len(qbits & set(t.GetOnBits())) for t in targets])
    b = np.array([t.GetNumOnBits() for t in targets])
    return q, targets, c, q.GetNumOnBits(), b


@pytest.mark.parametrize("ours,ref", [
    (metrics.tanimoto, DataStructs.TanimotoSimilarity),
    (metrics.dice, DataStructs.DiceSimilarity),
    (metrics.cosine, DataStructs.CosineSimilarity),
    (metrics.tversky(1.0, 0.0), lambda x, y: DataStructs.TverskySimilarity(x, y, 1.0, 0.0)),
    (metrics.tversky(0.3, 0.7), lambda x, y: DataStructs.TverskySimilarity(x, y, 0.3, 0.7)),
])
def test_metrics_match_rdkit(pairs, ours, ref):
    q, targets, c, a, b = pairs
    got = ours(c, a, b)
    expected = np.array([ref(q, t) for t in targets])
    assert got.dtype == np.float64 and np.allclose(got, expected, atol=1e-9)


def test_tversky_weights_are_query_first(pairs):
    q, targets, c, a, b = pairs
    assert not np.allclose(metrics.tversky(1, 0)(c, a, b), metrics.tversky(0, 1)(c, a, b))
    # a query that sits entirely inside a larger target scores 1.0 with (1, 0) and less with (0, 1)
    assert metrics.tversky(1, 0)(np.array([a]), a, np.array([a + 10]))[0] == 1.0
    assert metrics.tversky(0, 1)(np.array([a]), a, np.array([a + 10]))[0] < 1.0
    assert metrics.tversky(1, 0).__name__ == "tversky(1, 0)"


def test_zero_denominators_score_zero_and_unsigned_inputs_do_not_wrap():
    c, b = np.array([0, 0], dtype=np.uint64), np.array([0, 5], dtype=np.uint64)
    for m in (metrics.tanimoto, metrics.dice, metrics.cosine, metrics.tversky(1, 0)):
        assert m(c, 0, b).tolist() == [0.0, 0.0]
    # c > a + b can only come from a caller bug; unsigned inputs must not wrap into a huge score (0.0, as the denominator is negative)
    assert metrics.tanimoto(np.array([3], dtype=np.uint64), 1, np.array([1], dtype=np.uint64))[0] == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'molseal.metrics'`

- [ ] **Step 3: Write the module**

```python
# src/molseal/metrics.py
"""Similarity metrics as pure functions of three bit counts.

`c` is the per-row intersection count (array), `a` the query's bit count (int), `b` the per-row bit
count (array). Each metric returns float64 scores and gives 0.0 where its denominator is 0. Any
function with this shape is a valid metric for StructureIndex.similar / Vault.similar.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

Metric = Callable[[np.ndarray, int, np.ndarray], np.ndarray]


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    return np.divide(num, den, out=np.zeros_like(num), where=den > 0)


def tanimoto(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(c, a + b - c)


def dice(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(2 * c, a + b)


def cosine(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(c, np.sqrt(a * b))


def tversky(query_weight: float, target_weight: float) -> Metric:
    """`query_weight` (alpha) weights bits only in the query; `target_weight` (beta) weights bits only
    in the target. tversky(1, 0) = c / a: how much of the query is inside the target."""
    def metric(c, a, b) -> np.ndarray:
        c, b = _f(c), _f(b)
        return _ratio(c, query_weight * (a - c) + target_weight * (b - c) + c)
    metric.__name__ = f"tversky({query_weight}, {target_weight})"
    return metric
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_metrics.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add tests/test_metrics.py src/molseal/metrics.py
git commit -m "feat(metrics): Tanimoto, Dice, Cosine and Tversky as functions of three bit counts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- tests/test_metrics.py src/molseal/metrics.py
```

---

### Task 2: Fingerprint families in the engine

**Files:**
- Modify: `src/molseal/engine.py` (whole file replaced below, except `canonicalize`, `parse_many`, `_pack`, the delete and order code, which stay as they are)
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `molseal.metrics.Metric`, `tanimoto`.
- Produces: `FingerprintSpec(name, bits, generator)` with `.nbytes`; `MORGAN2`, `MORGAN2_CHIRAL`, `FEATMORGAN2`, `DEFAULT_FAMILIES`; `pack_fingerprints(generator, mols, nbytes, num_threads)`; `StructureIndex(families=DEFAULT_FAMILIES)` with `.families`, `add_family(spec, num_threads=-1)`, `drop_family(name)`, `fingerprints(ids, family) -> (list[UUID], ndarray)`, `similar(query_smiles, threshold=0.7, top_k=None, *, family="morgan2", metric=tanimoto)`, `to_parts()` emitting `fp:<name>` parts and meta `families`/`parts`, `from_parts(parts, families=DEFAULT_FAMILIES, num_threads=-1)`.
- Unchanged for this task: `substructure`, `count_substructure`, `mark_dead`, `add_batch` signature.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_engine.py`; keep every existing test)

```python
# append to tests/test_engine.py
from molseal import metrics
from molseal.engine import DEFAULT_FAMILIES, FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL, FingerprintSpec


def test_family_spec_validation():
    FingerprintSpec("ok-name_1", 64, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("Bad Name", 2048, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("x", 100, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("x", 0, lambda: None)
    assert MORGAN2.nbytes == 256 and DEFAULT_FAMILIES == (MORGAN2,)


def test_duplicate_unknown_and_last_family_rules():
    with pytest.raises(ValueError):
        engine.StructureIndex((MORGAN2, MORGAN2))
    with pytest.raises(ValueError):
        engine.StructureIndex(())
    idx = engine.StructureIndex()
    with pytest.raises(ValueError, match="unknown fingerprint family"):
        idx.similar("CCO", family="nope")
    with pytest.raises(ValueError, match="unknown fingerprint family"):
        idx.fingerprints([uuid4()], "nope")
    with pytest.raises(ValueError):
        idx.drop_family("morgan2")                       # the last one
    idx.add_family(FEATMORGAN2)
    assert [f.name for f in idx.families] == ["morgan2", "featmorgan2"]
    idx.drop_family("morgan2")
    assert [f.name for f in idx.families] == ["featmorgan2"]
    with pytest.raises(ValueError):
        idx.add_family(FingerprintSpec("featmorgan2", 1024, FEATMORGAN2.generator))   # same name, other width
    idx.add_family(FEATMORGAN2)                           # same name and width: no-op


def test_generator_width_mismatch_is_refused():
    wide = FingerprintSpec("narrow", 128, MORGAN2.generator)   # says 128, makes 2048
    idx = engine.StructureIndex((MORGAN2, wide))
    with pytest.raises(ValueError, match="2048-bit"):
        idx.add_batch([(uuid4(), "CCO", 1)])


def test_add_family_later_equals_ingest_bit_for_bit(dataset):
    ids, smiles, _ = dataset
    items = [(i, s, 1) for i, s in zip(ids, smiles)]
    full = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    full.add_batch(items)
    full.add_batch([(ids[0], "c1ccccc1Cl", 2)])         # an edit: position 0 dies
    full.mark_dead(ids[1], 2)                            # a delete
    full.add_batch([(ids[2], "C1CC", 2)])                # an unindexable edit
    later = engine.StructureIndex((MORGAN2,))
    later.add_batch(items)
    later.add_batch([(ids[0], "c1ccccc1Cl", 2)])
    later.mark_dead(ids[1], 2)
    later.add_batch([(ids[2], "C1CC", 2)])
    later.add_family(FEATMORGAN2)
    later.add_family(MORGAN2_CHIRAL)
    for fam in ("morgan2", "featmorgan2", "morgan2-chiral"):
        f1, m1 = full.fingerprints(ids, fam)
        f2, m2 = later.fingerprints(ids, fam)
        assert f1 == f2 and ids[1] not in f1 and ids[2] not in f1 and ids[0] in f1
        assert np.array_equal(m1, m2) and m1.shape == (len(f1), 256)
    assert later._fams["featmorgan2"].fps.shape[0] == later._cap     # grown to the index capacity


def test_fingerprints_follow_the_order_given_and_skip_unknown(index, dataset):
    ids, smiles, mols = dataset
    want = [ids[3], uuid4(), ids[1]]
    found, mat = index.fingerprints(want)
    assert found == [ids[3], ids[1]]
    assert np.array_equal(mat, engine.morgan_matrix([mols[3], mols[1]]))
    found, mat = index.fingerprints([])
    assert found == [] and mat.shape == (0, 256)


def _bulk(gen, fn, query, mols):
    qfp = gen.GetFingerprint(Chem.MolFromSmiles(query))
    return fn(qfp, [gen.GetFingerprint(m) for m in mols])


@pytest.mark.parametrize("family,metric,bulk", [
    (FEATMORGAN2, metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
    (MORGAN2_CHIRAL, metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
    (MORGAN2, metrics.dice, DataStructs.BulkDiceSimilarity),
    (MORGAN2, metrics.cosine, DataStructs.BulkCosineSimilarity),
    (MORGAN2, metrics.tversky(1.0, 0.0), lambda q, fps: DataStructs.BulkTverskySimilarity(q, fps, 1.0, 0.0)),
])
def test_similar_with_family_and_metric_matches_rdkit(dataset, family, metric, bulk):
    ids, smiles, mols = dataset
    idx = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    gen = family.generator()
    for query in ("CCOc1ccccc1", "NC(=O)c1ccccc1", "C[C@H](N)C(=O)O"):
        expected = {i: s for i, s in zip(ids, _bulk(gen, bulk, query, mols)) if s >= 0.3}
        got = idx.similar(query, 0.3, family=family.name, metric=metric)
        assert {i for i, _ in got} == set(expected)
        assert all(abs(s - expected[i]) < 1e-9 for i, s in got)
        assert [s for _, s in got] == sorted((s for _, s in got), reverse=True)


def test_chiral_family_separates_enantiomers_and_the_default_does_not(dataset):
    ids, smiles, _ = dataset
    idx = engine.StructureIndex((MORGAN2, MORGAN2_CHIRAL))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    canon = lambda x: Chem.MolToSmiles(Chem.MolFromSmiles(x))
    l_ala = {i for i, s in zip(ids, smiles) if s == canon("C[C@H](N)C(=O)O")}
    d_ala = {i for i, s in zip(ids, smiles) if s == canon("C[C@@H](N)C(=O)O")}
    assert l_ala and d_ala
    exact_default = {i for i, s in idx.similar("C[C@H](N)C(=O)O", 0.999)}
    exact_chiral = {i for i, s in idx.similar("C[C@H](N)C(=O)O", 0.999, family="morgan2-chiral")}
    assert d_ala <= exact_default and l_ala <= exact_chiral and not (d_ala & exact_chiral)


def test_parts_carry_every_family_and_missing_ones_are_recomputed(dataset):
    ids, smiles, _ = dataset
    items = [(i, s, 1) for i, s in zip(ids, smiles)]
    two = engine.StructureIndex((MORGAN2, FEATMORGAN2))
    two.add_batch(items)
    two.mark_dead(ids[0], 2)
    parts = two.to_parts()
    assert set(parts) == {"meta", "lib", "ids", "live", "rv", "fp:morgan2", "fp:featmorgan2"}
    same = engine.StructureIndex.from_parts(parts, (MORGAN2, FEATMORGAN2))
    fewer = engine.StructureIndex.from_parts(parts, (MORGAN2,))
    more = engine.StructureIndex.from_parts(parts, (FEATMORGAN2, MORGAN2_CHIRAL))
    assert [f.name for f in fewer.families] == ["morgan2"]
    assert [f.name for f in more.families] == ["featmorgan2", "morgan2-chiral"]
    fresh = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    fresh.add_batch(items)
    fresh.mark_dead(ids[0], 2)
    for idx, fam in ((same, "featmorgan2"), (more, "featmorgan2"), (more, "morgan2-chiral"), (fewer, "morgan2")):
        f1, m1 = idx.fingerprints(ids, fam)
        f2, m2 = fresh.fingerprints(ids, fam)
        assert f1 == f2 and np.array_equal(m1, m2)
    assert {i for i, _ in more.similar(smiles[7], 0.999, family="morgan2-chiral")} == {ids[7]}
    assert len(same) == len(ids) - 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_engine.py -q -k "family or fingerprints or metric or enantiomers or carry_every"`
Expected: FAIL with `ImportError: cannot import name 'FingerprintSpec'`

- [ ] **Step 3: Rewrite `src/molseal/engine.py`**

Keep `StructureError`, `canonicalize`, `parse_many`, `_pack`, `_parse_query`, `_ensure_order`, `_ensure_patterns`, `substructure`, `count_substructure`, `mark_dead`, `_kill` exactly as they are today. Replace the header, the constants, `morgan_matrix`, `__init__`, `_ensure_capacity`, `add_batch`, `similar`, `to_parts`, `from_parts`, and add the family methods, as follows:

```python
# src/molseal/engine.py — header and constants
"""In-memory structure index: RDKit SubstructLibrary for substructure, one packed fingerprint matrix
per registered family for similarity, and a live mask so edits and deletes work on an append-only
library.

No SQL lives here. Positions are integers; the vault maps ids to its own tables.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.rdSubstructLibrary import AddPatterns, CachedTrustedSmilesMolHolder, SubstructLibrary

from .metrics import Metric, tanimoto

RDLogger.DisableLog("rdApp.*")

QueryKind = Literal["smiles", "smarts"]
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


@dataclass(frozen=True)
class FingerprintSpec:
    """A fingerprint family. `name` is its identity: change it when the generator changes. `bits` is a
    positive multiple of 64. `generator` is a zero-argument factory returning an RDKit fingerprint
    generator: anything with GetFingerprints(mols, numThreads=n) giving `bits`-wide bit vectors."""
    name: str
    bits: int
    generator: Callable[[], Any]

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"bad fingerprint family name {self.name!r}")
        if self.bits <= 0 or self.bits % 64:
            raise ValueError(f"{self.name}: bits must be a positive multiple of 64, got {self.bits}")

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

FP_BITS = MORGAN2.bits            # the default family; kept for morgan_matrix callers
FP_BYTES = MORGAN2.nbytes
_MORGAN_GEN = MORGAN2.generator()
```

```python
# src/molseal/engine.py — replaces morgan_matrix (keep _pack above it)
def pack_fingerprints(generator: Any, mols: Sequence[Chem.Mol], nbytes: int, num_threads: int = -1) -> np.ndarray:
    """Fingerprints of `mols` from `generator`, packed to (n, nbytes) uint8. Bit i of a vector is
    np.unpackbits(row)[i]."""
    if not mols:
        return np.zeros((0, nbytes), dtype=np.uint8)
    fps = generator.GetFingerprints(list(mols), numThreads=num_threads)
    if fps[0].GetNumBits() != nbytes * 8:
        raise ValueError(f"generator produced {fps[0].GetNumBits()}-bit vectors, the family declares {nbytes * 8}")
    return np.stack([_pack(fp) for fp in fps])


def morgan_matrix(mols: Sequence[Chem.Mol], num_threads: int = -1) -> np.ndarray:
    """The default family (Morgan radius 2, 2048 bits) packed to (n, 256) uint8."""
    return pack_fingerprints(_MORGAN_GEN, mols, FP_BYTES, num_threads)


class _Family:
    __slots__ = ("spec", "gen", "fps", "pop")

    def __init__(self, spec: FingerprintSpec, cap: int) -> None:
        self.spec, self.gen = spec, spec.generator()
        self.fps = np.zeros((cap, spec.nbytes), dtype=np.uint8)
        self.pop = np.zeros(cap, dtype=np.uint16)

    def grow(self, cap: int, n: int) -> None:
        fps = np.zeros((cap, self.spec.nbytes), dtype=np.uint8); fps[:n] = self.fps[:n]
        pop = np.zeros(cap, dtype=np.uint16); pop[:n] = self.pop[:n]
        self.fps, self.pop = fps, pop

    def fill(self, start: int, mat: np.ndarray) -> None:
        self.fps[start : start + len(mat)] = mat
        self.pop[start : start + len(mat)] = np.bitwise_count(mat).sum(axis=1).astype(np.uint16)
```

```python
# src/molseal/engine.py — StructureIndex: constructor, families, capacity, ingest
class StructureIndex:
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
        self._family(name)
        if len(self._fams) == 1:
            raise ValueError("cannot drop the last fingerprint family")
        del self._fams[name]

    def _compute(self, fam: _Family, num_threads: int) -> None:
        """Fill `fam` for positions 0..n-1 from the library's own SMILES."""
        if self._n == 0:
            return
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

    # ----- bookkeeping -----   (keep __len__, positions, has, _ensure_order as today)

    def _ensure_capacity(self, extra: int) -> None:
        need = self._n + extra
        if need <= self._cap:
            return
        cap = max(need, self._cap * 2, 1024)
        live = np.zeros(cap, dtype=bool); live[: self._n] = self._live[: self._n]
        self._live, self._cap = live, cap
        for f in self._fams.values():
            f.grow(cap, self._n)

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
```

```python
# src/molseal/engine.py — similarity and parts
    # ----- similarity -----

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None, *,
                family: str = MORGAN2.name, metric: Metric = tanimoto) -> list[tuple[UUID, float]]:
        fam = self._family(family)
        if len(self) == 0:
            return []
        q = _parse_query(query_smiles, "smiles")
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
        sims[~self._live[:n]] = -1.0
        idx = np.flatnonzero(sims >= threshold)
        if top_k is not None and len(idx) > top_k:
            idx = idx[np.argpartition(-sims[idx], top_k - 1)[:top_k]]
        idx = idx[np.argsort(-sims[idx], kind="stable")]
        return [(self._ids[i], float(sims[i])) for i in idx]

    # ----- serialization (the snapshot module encrypts these parts) -----

    def to_parts(self) -> dict[str, bytes]:
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
                   num_threads: int = -1) -> "StructureIndex":
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
```

Delete the old `self._fps` / `self._pop` fields everywhere; nothing else references them (`grep -n "_fps\|_pop" src bench` must show only `_Family`).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest tests/test_engine.py tests/test_metrics.py -q`
Expected: all pass. Do not run `tests/test_snapshot.py` yet: `snapshot.py` still asks `to_parts()` for a `fps` part that no longer exists, and Task 3 replaces that module next. Say in the report that the snapshot tests were left red for Task 3.

- [ ] **Step 5: Commit**

```bash
git add src/molseal/engine.py tests/test_engine.py
git commit -m "feat(engine): fingerprint families as registered specs, metrics on one scan path, fingerprint accessor

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/engine.py tests/test_engine.py
```

---

### Task 3: Snapshot format 2

**Files:**
- Modify: `src/molseal/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `StructureIndex.to_parts()` (meta carries `parts` and `families`), `StructureIndex.from_parts(parts, families)`, `DEFAULT_FAMILIES`.
- Produces: `FORMAT_VERSION = 2`; `SnapshotFormatError(SnapshotError)`; `load(path, data_key, tenant_id, key_version, families=DEFAULT_FAMILIES)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_snapshot.py`; keep the existing ones)

```python
# append to tests/test_snapshot.py
import json
import numpy as np
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
from tests.mols import molecules


def make_two_family_index():
    smiles = molecules()[:60]
    ids = [uuid4() for _ in smiles]
    idx = engine.StructureIndex((MORGAN2, FEATMORGAN2))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    idx.mark_dead(ids[0], 2)
    idx.last_seq = 9
    return idx, ids, smiles


def test_format_two_round_trip_keeps_every_family(tmp_path):
    idx, ids, smiles = make_two_family_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1, chunk_size=4096)
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    assert json.loads(raw[12 : 12 + hlen])["format"] == 2
    back = snapshot.load(p, KEY, t, 1, (MORGAN2, FEATMORGAN2))
    assert [f.name for f in back.families] == ["morgan2", "featmorgan2"] and len(back) == len(ids) - 1
    for fam in ("morgan2", "featmorgan2"):
        f1, m1 = idx.fingerprints(ids, fam)
        f2, m2 = back.fingerprints(ids, fam)
        assert f1 == f2 and np.array_equal(m1, m2)
    assert {i for i, _ in back.similar(smiles[3], 0.999, family="featmorgan2")} >= {ids[3]}


def test_load_drops_unrequested_and_computes_missing_families(tmp_path):
    idx, ids, _ = make_two_family_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    snapshot.save(idx, p, KEY, t, 1)
    fewer = snapshot.load(p, KEY, t, 1, (MORGAN2,))
    assert [f.name for f in fewer.families] == ["morgan2"]
    more = snapshot.load(p, KEY, t, 1, (MORGAN2, MORGAN2_CHIRAL))
    assert [f.name for f in more.families] == ["morgan2", "morgan2-chiral"]
    fresh = engine.StructureIndex((MORGAN2_CHIRAL,))
    fresh.add_batch([(i, s, 1) for i, s in zip(ids, molecules()[:60])])
    fresh.mark_dead(ids[0], 2)
    f1, m1 = more.fingerprints(ids, "morgan2-chiral")
    f2, m2 = fresh.fingerprints(ids, "morgan2-chiral")
    assert f1 == f2 and np.array_equal(m1, m2)
    assert snapshot.load(p, KEY, t, 1).families == (MORGAN2,)      # the default


def test_a_format_one_file_raises_the_format_error_only(tmp_path, monkeypatch):
    idx, _, _ = make_index()
    t = uuid4()
    p = tmp_path / "t.molseal"
    monkeypatch.setattr(snapshot, "FORMAT_VERSION", 1)
    snapshot.save(idx, p, KEY, t, 1)
    monkeypatch.undo()
    assert snapshot.FORMAT_VERSION == 2
    with pytest.raises(snapshot.SnapshotFormatError):
        snapshot.load(p, KEY, t, 1)
    # a header edited to claim format 2 on format-1 content is tampering, not a version mismatch
    raw = p.read_bytes()
    (hlen,) = struct.unpack(">I", raw[8:12])
    edited = raw[:12] + raw[12 : 12 + hlen].replace(b'"format": 1', b'"format": 2') + raw[12 + hlen :]
    assert edited != raw
    p.write_bytes(edited)
    with pytest.raises(snapshot.SnapshotError) as info:
        snapshot.load(p, KEY, t, 1)
    assert not isinstance(info.value, snapshot.SnapshotFormatError)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_snapshot.py -q`
Expected: the three new tests FAIL (`AttributeError: ... SnapshotFormatError` / `KeyError: 'fps'`), and the pre-existing round-trip tests also FAIL with `KeyError: 'fps'` until Step 3.

- [ ] **Step 3: Change `src/molseal/snapshot.py`**

```python
# src/molseal/snapshot.py — replace the constants, save() blob assembly, and load() tail
from .engine import DEFAULT_FAMILIES, FingerprintSpec, StructureIndex

FORMAT_VERSION = 2
MAGIC = b"MOLSEAL1"


class SnapshotError(Exception):
    pass


class SnapshotFormatError(SnapshotError):
    """A molseal snapshot of another format version. Safe to ignore and rebuild; never tampering."""
```

In `save`, replace the `blob = ...` line with:

```python
    order = ["meta"] + json.loads(parts["meta"])["parts"]
    blob = b"".join(struct.pack(">Q", len(parts[k])) + parts[k] for k in order)
```

In `load`, add the `families` parameter and replace the format check and the part-splitting tail:

```python
def load(path: Path, data_key: bytes, tenant_id: UUID, key_version: int,
         families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> StructureIndex:
    ...
    if header.get("format") != FORMAT_VERSION:
        raise SnapshotFormatError(f"snapshot format {header.get('format')!r}; this build reads {FORMAT_VERSION}")
    ...   # tenant/key checks, chunk parsing and open_chunks unchanged
    parts: dict[str, bytes] = {}
    off = 0

    def take() -> bytes:
        nonlocal off
        (plen,) = struct.unpack(">Q", blob[off : off + 8])
        off += 8
        part = blob[off : off + plen]
        off += plen
        return part

    parts["meta"] = take()
    for k in json.loads(parts["meta"])["parts"]:
        parts[k] = take()
    index = StructureIndex.from_parts(parts, families)
    index.last_seq = int(header["last_seq"])
    return index
```

Remove `PART_ORDER`. Add `from collections.abc import Sequence` to the imports.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_snapshot.py tests/test_engine.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/molseal/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): format 2 with one part per fingerprint family; old files raise a format error

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/snapshot.py tests/test_snapshot.py
```

---

### Task 4: Substructure options

**Files:**
- Modify: `src/molseal/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Produces: `QueryKind = Literal["smiles", "smarts", "molblock"]`; `substructure(query, kind="smiles", max_results=None, num_threads=-1, *, generalized=False, use_chirality=False)`; `count_substructure(query, kind="smiles", num_threads=-1, *, generalized=False, use_chirality=False)`; the pattern screen is a `TautomerPatternHolder`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_engine.py`)

```python
# append to tests/test_engine.py
def brute_force_chiral(dataset, query):
    ids, _, mols = dataset
    q = Chem.MolFromSmiles(query)
    return {i for i, m in zip(ids, mols) if m.HasSubstructMatch(q, useChirality=True)}


def test_generalized_finds_the_tautomer_and_plain_does_not():
    idx = engine.StructureIndex()
    keto, enol, other = uuid4(), uuid4(), uuid4()
    idx.add_batch([(keto, "O=c1cccc[nH]1", 1), (enol, "Oc1ccccn1", 1), (other, "c1ccccc1", 1)])
    assert set(idx.substructure("Oc1ccccn1")) == {enol}
    assert set(idx.substructure("Oc1ccccn1", generalized=True)) == {keto, enol}
    assert idx.count_substructure("Oc1ccccn1", generalized=True) == 2
    assert idx.count_substructure("Oc1ccccn1") == 1
    assert idx._lib.GetFpHolder().__class__.__name__ == "TautomerPatternHolder"


def test_use_chirality_matches_only_the_same_defined_stereo(index, dataset):
    ids, smiles, _ = dataset
    q = "C[C@H](N)C(=O)O"
    expected_on = brute_force_chiral(dataset, q)
    assert set(index.substructure(q, use_chirality=True)) == expected_on
    assert index.count_substructure(q, use_chirality=True) == len(expected_on)
    assert expected_on < brute_force(dataset, q, "smiles")                 # strict subset: the enantiomer and the flat one drop out
    flat = "CC(N)C(=O)O"
    assert set(index.substructure(flat, use_chirality=True)) == brute_force(dataset, flat, "smiles")   # undefined query matches all


def test_molblock_query_with_explicit_hydrogens_and_kekule_ring(index):
    block = Chem.MolToMolBlock(Chem.AddHs(Chem.MolFromSmiles("Oc1ccccc1")), kekulize=True)
    assert "H " in block and "M  END" in block
    assert set(index.substructure(block, kind="molblock")) == set(index.substructure("Oc1ccccc1"))
    assert index.count_substructure(block, kind="molblock") == index.count_substructure("Oc1ccccc1")
    with pytest.raises(engine.StructureError):
        index.substructure("not a molblock", kind="molblock")
    with pytest.raises(engine.StructureError):
        index.substructure("CCO", kind="inchi")


def test_generalized_with_smarts_or_molblock_answers_or_raises_cleanly(index):
    for query, kind in (("[#6]-[#7]-[#6](=O)", "smarts"), (Chem.MolToMolBlock(Chem.MolFromSmiles("NC(=O)C")), "molblock")):
        try:
            hits = index.substructure(query, kind=kind, generalized=True)
        except engine.StructureError:
            continue
        assert set(hits) >= set(index.substructure(query, kind=kind))


def test_capped_search_is_deterministic_on_one_thread(index):
    runs = {tuple(index.substructure("c1ccccc1", max_results=10, num_threads=1)) for _ in range(5)}
    assert len(runs) == 1 and len(next(iter(runs))) == 10
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_engine.py -q -k "generalized or chirality or molblock or deterministic"`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'generalized'`, `StructureError` on molblock kind).

- [ ] **Step 3: Change `src/molseal/engine.py`**

Imports:

```python
from rdkit.Chem.rdGeneralizedSubstruct import CreateExtendedQueryMol
from rdkit.Chem.rdSubstructLibrary import AddPatterns, CachedTrustedSmilesMolHolder, SubstructLibrary, TautomerPatternHolder
```

Query kinds and parsing (replace `QueryKind` and `_parse_query`):

```python
QueryKind = Literal["smiles", "smarts", "molblock"]
_PARSERS: dict[str, Callable[[str], Chem.Mol | None]] = {
    "smiles": Chem.MolFromSmiles,
    "smarts": Chem.MolFromSmarts,            # taken literally: no aromaticity perception
    "molblock": Chem.MolFromMolBlock,        # sanitized, explicit hydrogens removed
}


def _parse_query(query: str, kind: QueryKind, generalized: bool = False):
    parser = _PARSERS.get(kind)
    if parser is None:
        raise StructureError(f"unknown query kind {kind!r}")
    try:
        q = parser(query)
    except (ValueError, RuntimeError):
        q = None
    if q is None:
        raise StructureError(f"cannot parse {kind} query: {query!r}")
    if not generalized:
        return q
    try:   # positional: doEnumeration=True, doTautomers=True, adjustQueryProperties=False (the cartridge's defaults)
        return CreateExtendedQueryMol(q, True, True, False)
    except (ValueError, RuntimeError) as exc:
        raise StructureError(f"cannot build a generalized query from {kind} {query!r}: {exc}") from exc
```

Pattern screen (replace `_ensure_patterns`):

```python
    def _ensure_patterns(self, num_threads: int) -> None:
        # The tautomer holder screens plain and generalized queries alike; the plain holder would
        # skip the screen for generalized queries and scan every molecule.
        if not self._patterns and self._n > 0:
            AddPatterns(self._lib, TautomerPatternHolder(), numThreads=num_threads)
            self._patterns = True
```

Searches (replace both signatures and their `_parse_query` / `GetMatches` / `CountMatches` calls):

```python
    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None,
                     num_threads: int = -1, *, generalized: bool = False, use_chirality: bool = False) -> list[UUID]:
        if len(self) == 0:
            return []
        q = _parse_query(query, kind, generalized)
        self._ensure_patterns(num_threads)
        self._ensure_order()
        hits = self._lib.GetMatches(q, recursionPossible=True, useChirality=use_chirality, useQueryQueryMatches=False,
                                    numThreads=num_threads, maxResults=-1 if max_results is None else int(max_results))
        out: dict[UUID, None] = {}
        for pos in hits:
            id_ = self._ids[pos]
            if self._live[pos] and self._pos_of[id_] == pos:
                out[id_] = None
        return list(out)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", num_threads: int = -1, *,
                           generalized: bool = False, use_chirality: bool = False) -> int:
        if len(self) == 0:
            return 0
        q = _parse_query(query, kind, generalized)
        self._ensure_patterns(num_threads)
        self._ensure_order()
        return int(self._lib.CountMatches(q, recursionPossible=True, useChirality=use_chirality,
                                          useQueryQueryMatches=False, numThreads=num_threads))
```

- [ ] **Step 4: Run the engine and snapshot tests**

Run: `uv run pytest tests/test_engine.py tests/test_snapshot.py -q`
Expected: all pass. The snapshot round trip now carries a tautomer holder; the existing round-trip test proves it deserializes.

- [ ] **Step 5: Commit**

```bash
git add src/molseal/engine.py tests/test_engine.py
git commit -m "feat(engine): molblock queries, generalized matching and chirality options on the tautomer pattern holder

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/engine.py tests/test_engine.py
```

---

### Task 5: Vault: families, bulk reads, option pass-through, rebuild on an old snapshot

**Files:**
- Modify: `src/molseal/vault.py`
- Test: `tests/test_vault.py`

**Interfaces:**
- Consumes: `StructureIndex(families)`, `snapshot.load(..., families)`, `snapshot.SnapshotFormatError`, `DEFAULT_FAMILIES`, `Metric`, `tanimoto`.
- Produces: `Vault(..., families=DEFAULT_FAMILIES)` with `.families`; `get_many(ids) -> dict[UUID, Payload]`; `fingerprints(ids, family="morgan2") -> (list[UUID], ndarray)`; `substructure(query, kind="smiles", max_results=None, *, generalized=False, use_chirality=False)`; `count_substructure(query, kind="smiles", *, generalized=False, use_chirality=False)`; `similar(query_smiles, threshold=0.7, top_k=None, *, family="morgan2", metric=tanimoto)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_vault.py`)

```python
# append to tests/test_vault.py
import numpy as np
from molseal import metrics, snapshot
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL


def test_get_many_decrypts_live_rows_and_skips_deleted_and_unknown(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a, b, c = uuid4(), uuid4(), uuid4()
    v1.put_many([(a, "OCC", {"k": "1"}), (b, "CCN", None), (c, "c1ccccc1", None)])
    Vault(store, kp, t).delete(b)                                   # a second instance deletes b
    got = v1.get_many([a, b, c, uuid4()])
    assert set(got) == {a, c} and got[a].smiles == "CCO" and got[a].keys == {"k": "1"}
    assert v1.get_many([]) == {}


def test_fingerprints_sync_before_reading(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t)
    a, b = uuid4(), uuid4()
    v1.put(a, "CCO")
    Vault(store, kp, t).put(b, "CCN")                                # written elsewhere
    found, mat = v1.fingerprints([b, a])
    assert found == [b, a]
    assert np.array_equal(mat, engine.morgan_matrix([engine.Chem.MolFromSmiles("CCN"), engine.Chem.MolFromSmiles("CCO")]))
    with pytest.raises(ValueError):
        v1.fingerprints([a], "nope")


def test_families_reach_fresh_rebuilt_and_loaded_indexes(env):
    store, kp, snap = env
    t = uuid4()
    v = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2, FEATMORGAN2))
    a = uuid4()
    v.put(a, "CCOc1ccccc1")
    assert [f.name for f in v.index.families] == ["morgan2", "featmorgan2"]
    assert {i for i, _ in v.similar("CCOc1ccccc1", 0.99, family="featmorgan2", metric=metrics.dice)} == {a}
    v.rebuild()
    assert [f.name for f in v.index.families] == ["morgan2", "featmorgan2"]
    v.save_snapshot()
    same = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2, FEATMORGAN2))
    assert [f.name for f in same.index.families] == ["morgan2", "featmorgan2"]
    other = Vault(store, kp, t, snapshot_dir=snap, families=(MORGAN2_CHIRAL,))
    assert [f.name for f in other.index.families] == ["morgan2-chiral"]
    assert {i for i, _ in other.similar("CCOc1ccccc1", 0.99, family="morgan2-chiral")} == {a}


def test_a_format_one_snapshot_is_rebuilt_and_resaved_as_format_two(env, monkeypatch):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    a = uuid4()
    v1.put(a, "CCO")
    monkeypatch.setattr(snapshot, "FORMAT_VERSION", 1)
    path = v1.save_snapshot()
    monkeypatch.undo()
    v2 = Vault(store, kp, t, snapshot_dir=snap)                      # must not raise
    assert v2.substructure("CCO") == [a]
    v2.save_snapshot()
    assert snapshot.load(path, v2.keys.data_key, t, v2.keys.data_key_version).substructure("CCO") == [a]


def test_a_tampered_snapshot_still_refuses_to_open(env):
    store, kp, snap = env
    t = uuid4()
    v1 = Vault(store, kp, t, snapshot_dir=snap)
    v1.put(uuid4(), "CCO")
    path = v1.save_snapshot()
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(bytes(raw))
    with pytest.raises(snapshot.SnapshotError):
        Vault(store, kp, t, snapshot_dir=snap)


def test_substructure_options_pass_through(vault):
    keto, enol, l_ala, d_ala = uuid4(), uuid4(), uuid4(), uuid4()
    vault.put_many([(keto, "O=c1cccc[nH]1", None), (enol, "Oc1ccccn1", None),
                    (l_ala, "C[C@H](N)C(=O)O", None), (d_ala, "C[C@@H](N)C(=O)O", None)])
    assert set(vault.substructure("Oc1ccccn1", generalized=True)) == {keto, enol}
    assert vault.count_substructure("Oc1ccccn1", generalized=True) == 2
    assert vault.substructure("C[C@H](N)C(=O)O", use_chirality=True) == [l_ala]
    assert set(vault.substructure("C[C@H](N)C(=O)O")) == {l_ala, d_ala}
    block = engine.Chem.MolToMolBlock(engine.Chem.MolFromSmiles("Oc1ccccn1"))
    assert vault.substructure(block, kind="molblock") == [enol]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal uv run pytest tests/test_vault.py -q`
Expected: the six new tests FAIL (`AttributeError: 'Vault' object has no attribute 'get_many'`, `TypeError: ... 'families'`, `SnapshotFormatError` raised from the constructor).

- [ ] **Step 3: Change `src/molseal/vault.py`**

```python
# imports
from collections.abc import Iterable, Sequence
from itertools import batched
import numpy as np

from . import snapshot
from .engine import DEFAULT_FAMILIES, FingerprintSpec, QueryKind, StructureIndex, canonicalize
from .metrics import Metric, tanimoto
from .snapshot import SnapshotFormatError
```

Constructor (add the parameter, store the tuple, build with it):

```python
    def __init__(self, store: StructureStore, key_provider: KeyProvider, tenant_id: UUID,
                 snapshot_dir: Path | None = None, num_threads: int = -1, refresh_batch: int = 10000,
                 families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES) -> None:
        ...
        self.families = tuple(families)
        ...
        self.index = self._load_snapshot() or StructureIndex(self.families)
        self.refresh()
```

Snapshot load (format mismatch is "no snapshot"; everything else still raises):

```python
    def _load_snapshot(self) -> StructureIndex | None:
        p = self._snapshot_path()
        if p is None or not p.exists():
            return None
        try:
            return snapshot.load(p, self.keys.data_key, self.tenant_id, self.keys.data_key_version, self.families)
        except SnapshotFormatError:
            return None          # an older format: rebuild from the store, the next save overwrites it
```

Reads (replace the three search pass-throughs; add the two bulk reads):

```python
    def get_many(self, ids: Iterable[UUID]) -> dict[UUID, Payload]:
        """Decrypted payloads of the live rows among `ids`; deleted and unknown ids are absent."""
        out: dict[UUID, Payload] = {}
        for group in batched(ids, 10_000):
            for id_, rec in self.store.get_structures(self.tenant_id, list(group)).items():
                p = self._open(rec)
                if not p.deleted:
                    out[id_] = p
        return out

    def fingerprints(self, ids: Iterable[UUID], family: str = DEFAULT_FAMILIES[0].name) -> tuple[list[UUID], np.ndarray]:
        """Packed fingerprint rows of `family` for the live ids among `ids`, in the order given, after
        the same catch-up a search does."""
        ids = list(ids)
        with self._lock:
            self._sync_locked()
            return self.index.fingerprints(ids, family)

    def substructure(self, query: str, kind: QueryKind = "smiles", max_results: int | None = None, *,
                     generalized: bool = False, use_chirality: bool = False) -> list[UUID]:
        with self._lock:
            self._sync_locked()
            return self.index.substructure(query, kind, max_results, self.num_threads,
                                           generalized=generalized, use_chirality=use_chirality)

    def count_substructure(self, query: str, kind: QueryKind = "smiles", *,
                           generalized: bool = False, use_chirality: bool = False) -> int:
        with self._lock:
            self._sync_locked()
            return self.index.count_substructure(query, kind, self.num_threads,
                                                 generalized=generalized, use_chirality=use_chirality)

    def similar(self, query_smiles: str, threshold: float = 0.7, top_k: int | None = None, *,
                family: str = DEFAULT_FAMILIES[0].name, metric: Metric = tanimoto) -> list[tuple[UUID, float]]:
        with self._lock:
            self._sync_locked()
            return self.index.similar(query_smiles, threshold, top_k, family=family, metric=metric)
```

Rebuild (one line):

```python
    def _rebuild_locked(self, upto: int) -> None:
        fresh = StructureIndex(self.families)          # built aside, then swapped in
        self._catch_up(fresh, upto)
        self.index = fresh
```

- [ ] **Step 4: Run the whole suite**

Run: `MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal uv run pytest -q`
Expected: all pass (the CLI and bench unit tests keep working: every call they make is positional-compatible).

- [ ] **Step 5: Commit**

```bash
git add src/molseal/vault.py tests/test_vault.py
git commit -m "feat(vault): fingerprint families, bulk decrypted reads, search options, rebuild on an older snapshot

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/vault.py tests/test_vault.py
```

---

### Task 6: Parity gates 5–10 and the 500k parity run

**Files:**
- Modify: `bench/queries.py`, `bench/parity.py`
- Test: `tests/bench/test_queries.py`, `tests/bench/test_parity.py`
- Create: `docs/results/2026-10-14-500k-search-surface/README.md` (+ `parity_500000.json`, `queries_500000.json` copied from the run)
- Modify: `docs/results/README.md`

**Interfaces:**
- Consumes: `Vault(families=...)`, `vault.similar(..., family=, metric=)`, `vault.substructure(..., generalized=, use_chirality=)`, `vault.get_many`, `vault.fingerprints`, `engine.pack_fingerprints`, `molseal.metrics`.
- Produces: `queries.mirror(smiles)`, `queries.chiral_queries(smiles_list, rng, n=20, max_atoms=30)`, a `"chiral"` list in the query file; `parity.FAMILIES`, `parity.ensure_feature_column`, `parity.check_chiral`, `parity.check_rows`, `parity.reference_fingerprints`, gates 5–10 in `parity.run` and `overall_ok`.

- [ ] **Step 1: Write the failing query tests** (append to `tests/bench/test_queries.py`)

```python
# append to tests/bench/test_queries.py
def test_chiral_queries_have_assigned_centres_and_mirror_images():
    out = q.chiral_queries(molecules(), random.Random(3), n=20)
    assert len(out) >= 2
    for s in out:
        m = Chem.MolFromSmiles(s)
        assert m is not None and m.GetNumHeavyAtoms() <= 30
        assert Chem.FindMolChiralCenters(m, includeUnassigned=False, useLegacyImplementation=False)
    l_ala = Chem.MolToSmiles(Chem.MolFromSmiles("C[C@H](N)C(=O)O"))
    d_ala = Chem.MolToSmiles(Chem.MolFromSmiles("C[C@@H](N)C(=O)O"))
    assert q.mirror(l_ala) == d_ala and q.mirror(d_ala) == l_ala
    assert q.mirror("not a molecule") is None
    assert out == q.chiral_queries(molecules(), random.Random(3), n=20)        # seeded


def test_chiral_queries_are_drawn_after_the_rest_so_old_sections_do_not_move(tmp_path, monkeypatch):
    slice_path = tmp_path / "slice_438.tsv"
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tK{i}\n" for i, s in enumerate(molecules())))
    with_chiral = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    monkeypatch.setattr(q, "chiral_queries", lambda *a, **k: [])
    without = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    assert with_chiral["substructure"] == without["substructure"]
    assert with_chiral["similarity"] == without["similarity"]
    assert with_chiral["chiral"] and without["chiral"] == []
    assert all(c["id"].startswith("chi-") and c["query"] for c in with_chiral["chiral"])
```

- [ ] **Step 2: Write the failing parity tests** (append to `tests/bench/test_parity.py`)

```python
# append to tests/bench/test_parity.py
import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

from molseal import metrics


def test_check_chiral_requires_equal_on_sets_and_supersets_on_both_sides():
    a, b, r = uuid4(), uuid4(), uuid4()
    ok = parity.check_chiral("q", [a, r], {a}, [a, b, r], {a, b}, {r})
    assert ok["ok"] and ok["equal"] and ok["superset_molseal"] and ok["superset_cartridge"]
    assert not parity.check_chiral("q", [a], {b}, [a, b], {a, b}, set())["ok"]           # on sets differ
    assert not parity.check_chiral("q", [a], {a}, [b], {a, b}, set())["ok"]              # molseal off is not a superset
    assert not parity.check_chiral("q", [a], {a}, [a, b], {b}, set())["ok"]              # cartridge off is not a superset


def test_check_rows_counts_mismatches_and_missing_ids():
    a, b = uuid4(), uuid4()
    ref = {a: np.array([1, 2], dtype=np.uint8), b: np.array([3, 4], dtype=np.uint8)}
    assert parity.check_rows([a, b], np.array([[1, 2], [3, 4]], dtype=np.uint8), ref)["ok"]
    bad = parity.check_rows([a, b], np.array([[1, 2], [9, 4]], dtype=np.uint8), ref)
    assert not bad["ok"] and bad["mismatches"] == 1
    missing = parity.check_rows([a], np.array([[1, 2]], dtype=np.uint8), ref)
    assert not missing["ok"] and missing["missing"] == 1


def test_gate_six_would_catch_a_swapped_tversky_pair():
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    query = gen.GetFingerprint(Chem.MolFromSmiles("c1ccccc1"))
    targets = [gen.GetFingerprint(Chem.MolFromSmiles(s)) for s in ("c1ccccc1CCN", "CCOc1ccccc1C(=O)O", "CCCC")]
    ids = [uuid4() for _ in targets]
    qbits = set(query.GetOnBits())
    c = np.array([len(qbits & set(t.GetOnBits())) for t in targets])
    b = np.array([t.GetNumOnBits() for t in targets])
    ours = dict(zip(ids, metrics.tversky(1, 0)(c, query.GetNumOnBits(), b)))
    right = {i: DataStructs.TverskySimilarity(query, t, 1.0, 0.0) for i, t in zip(ids, targets)}   # query first
    swapped = {i: DataStructs.TverskySimilarity(t, query, 1.0, 0.0) for i, t in zip(ids, targets)}
    assert parity.compare_scores(ours, right)["equal"]
    assert not parity.compare_scores(ours, swapped)["equal"]
    assert parity.SQL_TVERSKY.count("tversky_sml(morganbv_fp(mol_from_smiles(") == 2       # the query is the first argument


def test_overall_ok_covers_gates_five_to_ten():
    good = {"gate1": {"ok": True}, **{f"gate{n}": [{"ok": True}] for n in range(2, 11)}}
    assert parity.overall_ok(good)
    for n in range(5, 11):
        assert not parity.overall_ok({**good, f"gate{n}": [{"ok": True}, {"ok": False}]})
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/bench/test_queries.py tests/bench/test_parity.py -q`
Expected: the six new tests FAIL with `AttributeError` (`chiral_queries`, `mirror`, `check_chiral`, `check_rows`, `SQL_TVERSKY`).

- [ ] **Step 4: Add the chiral queries to `bench/queries.py`**

```python
# bench/queries.py — add after fragments_from
def mirror(smiles: str) -> str | None:
    """The mirror image: every @@ becomes @ and every @ becomes @@. None if it does not re-parse."""
    flipped = smiles.replace("@@", "\0").replace("@", "@@").replace("\0", "@")
    m = Chem.MolFromSmiles(flipped)
    return None if m is None else Chem.MolToSmiles(m)


def chiral_queries(smiles_list: list[str], rng: random.Random, n: int = 20, max_atoms: int = 30) -> list[str]:
    """n seeded molecules with at least one assigned tetrahedral stereocentre and at most max_atoms heavy
    atoms, each followed by its mirror image when that differs. Canonical SMILES."""
    pool: set[str] = set()
    for smi in smiles_list:
        mol = Chem.MolFromSmiles(smi)
        if mol is None or mol.GetNumHeavyAtoms() > max_atoms:
            continue
        if Chem.FindMolChiralCenters(mol, includeUnassigned=False, useLegacyImplementation=False):
            pool.add(Chem.MolToSmiles(mol))
    out: list[str] = []
    for s in rng.sample(sorted(pool), min(n, len(pool))):
        out.append(s)
        m = mirror(s)
        if m is not None and m != s:
            out.append(m)
    return out
```

In `build_queries`, add the parameter `n_chiral: int = 20` and, after the `sim = [...]` line (so the earlier random draws do not move):

```python
    chiral = [{"id": f"chi-{i:03d}", "query": s} for i, s in enumerate(chiral_queries([r[1] for r in picked], rng, n=n_chiral))]
    return {"seed": seed, "slice": str(slice_path), "substructure": sub, "similarity": sim, "chiral": chiral}
```

Update `main`'s print to include `len(qs['chiral'])` chiral queries.

- [ ] **Step 5: Add gates 5–10 to `bench/parity.py`**

Imports to add:

```python
from itertools import batched
import numpy as np
from molseal import engine, metrics
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
```

Constants and helpers (after `_GEN`):

```python
FAMILIES = (MORGAN2, MORGAN2_CHIRAL, FEATMORGAN2)
GATE5_THRESHOLDS = (0.55, 0.7)
_Q = "mol_from_smiles(CAST(:q AS cstring))"
SQL_FEAT = (f"SELECT id, tanimoto_sml(fmfp, featmorganbv_fp({_Q}, 2)) AS s FROM mols "
            f"WHERE fmfp IS NOT NULL AND tanimoto_sml(fmfp, featmorganbv_fp({_Q}, 2)) >= :t")
SQL_TVERSKY = (f"SELECT id, tversky_sml(morganbv_fp({_Q}), mfp, CAST(1.0 AS real), CAST(0.0 AS real)) AS s FROM mols "
               f"WHERE mfp IS NOT NULL AND tversky_sml(morganbv_fp({_Q}), mfp, CAST(1.0 AS real), CAST(0.0 AS real)) >= :t")
SQL_DICE = (f"SELECT id, dice_sml(mfp, morganbv_fp({_Q})) AS s FROM mols "
            f"WHERE mfp IS NOT NULL AND dice_sml(mfp, morganbv_fp({_Q})) >= :t")
SQL_GEN_IDS = f"SELECT id FROM mols WHERE m @>> mol_to_xqmol({_Q})"
SQL_GEN_COUNT = f"SELECT count(*) FROM mols WHERE m @>> mol_to_xqmol({_Q})"


def ensure_feature_column(conn) -> None:
    """Gate 5 compares against the cartridge's feature-Morgan fingerprint; the column is added once."""
    conn.execute(text("ALTER TABLE mols ADD COLUMN IF NOT EXISTS fmfp bfp"))
    conn.execute(text("UPDATE mols SET fmfp = featmorganbv_fp(m, 2) WHERE fmfp IS NULL AND m IS NOT NULL"))
    conn.commit()


def _cart_scores(conn, sql: str, query: str, threshold: float) -> dict[uuid.UUID, float]:
    return {r.id: float(r.s) for r in conn.execute(text(sql), {"q": query, "t": threshold})}


def check_chiral(qid: str, on_ms: list, on_cart: set, off_ms: list, off_cart: set, rejected: set) -> dict:
    """Gate 8 for one query: chirality-on id sets agree, and chirality-off is a superset on both sides."""
    on = {i for i in on_ms if i not in rejected}
    off = {i for i in off_ms if i not in rejected}
    cmp = compare_sets(on, on_cart)
    return {"id": qid, **cmp, "superset_molseal": on <= off, "superset_cartridge": on_cart <= off_cart,
            "ok": cmp["equal"] and on <= off and on_cart <= off_cart}


def check_rows(found: list, mat: np.ndarray, reference: dict) -> dict:
    """Gate 10 fingerprint bytes: every row handed out equals the row computed by RDKit in Python."""
    mism = sum(1 for i, row in zip(found, mat) if not np.array_equal(row, reference[i]))
    missing = len(reference) - len(found)
    return {"compared": len(found), "mismatches": mism, "missing": missing, "ok": mism == 0 and missing == 0}


def reference_fingerprints(vault: Vault, ids: list[uuid.UUID], gens: dict) -> tuple[list[uuid.UUID], dict[str, list]]:
    """RDKit bit vectors for every live id, computed in Python from the decrypted SMILES (get_many)."""
    found: list[uuid.UUID] = []
    fps: dict[str, list] = {name: [] for name in gens}
    for group in batched(ids, 10_000):
        for i, p in vault.get_many(group).items():
            m = Chem.MolFromSmiles(p.smiles)
            if m is None:
                continue
            found.append(i)
            for name, g in gens.items():
                fps[name].append(g.GetFingerprint(m))
    return found, fps


def overall_ok(out: dict) -> bool:
    lists = tuple(f"gate{n}" for n in range(2, 11))
    return out["gate1"]["ok"] and all(r["ok"] for g in lists for r in out.get(g, []))
```

`run` gains `all_ids: list[uuid.UUID]` after `sample_ids`, initialises `out` with `gate5`…`gate10` lists, and appends after the gate-4 loop, still inside the `with cart_engine.connect() as conn:` block:

```python
        # Gates 5 and 6: another family, and two more metrics, against the cartridge
        ensure_feature_column(conn)
        for sq in queries["similarity"]:
            q, src = sq["query"], uuid.UUID(sq["source_id"])
            for t in GATE5_THRESHOLDS:
                rec = check_similarity(sq["id"], vault.similar(q, t, family="featmorgan2"), _cart_scores(conn, SQL_FEAT, q, t), src, rejected)
                out["gate5"].append({"threshold": t, **rec})
            for name, metric, sql in (("tversky(1, 0)", metrics.tversky(1, 0), SQL_TVERSKY), ("dice", metrics.dice, SQL_DICE)):
                rec = check_similarity(sq["id"], vault.similar(q, threshold, metric=metric), _cart_scores(conn, sql, q, threshold), src, rejected)
                out["gate6"].append({"metric": name, **rec})
        # Gate 7: generalized substructure against @>>; gate 9: molblock self-consistency. SMILES queries only.
        for sq in queries["substructure"]:
            if sq["kind"] != "smiles":
                continue
            q = sq["query"]
            g2, g3 = check_substructure(sq["id"], vault.substructure(q, generalized=True), vault.count_substructure(q, generalized=True),
                                        int(conn.execute(text(SQL_GEN_COUNT), {"q": q}).scalar_one()), rejected,
                                        lambda: {r.id for r in conn.execute(text(SQL_GEN_IDS), {"q": q})}, id_set_limit)
            out["gate7"].append({**g2, "ids": g3, "ok": g2["ok"] and g3["ok"]})
            block = Chem.MolToMolBlock(Chem.MolFromSmiles(q))
            cmp = compare_sets(set(vault.substructure(block, kind="molblock")), set(vault.substructure(q)))
            out["gate9"].append({"id": sq["id"], **cmp, "ok": cmp["equal"]})
        # Gate 8: chirality against rdkit.do_chiral_sss
        for cq in queries.get("chiral", []):
            q = cq["query"]
            conn.execute(text("SET rdkit.do_chiral_sss = true"))
            on_cart = _cart_ids(conn, "smiles", q)
            conn.execute(text("SET rdkit.do_chiral_sss = false"))
            off_cart = _cart_ids(conn, "smiles", q)
            out["gate8"].append(check_chiral(cq["id"], vault.substructure(q, use_chirality=True), on_cart, vault.substructure(q), off_cart, rejected))
```

and after the `with` block, before `zero_hit_queries`:

```python
    # Gate 10: what the cartridge cannot check, against RDKit in Python. The reference holds the
    # rejected rows too, so nothing is filtered on either side.
    gens = {s.name: s.generator() for s in FAMILIES}
    found, ref = reference_fingerprints(vault, all_ids, gens)
    legs = (("morgan2", "tanimoto", metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
            ("morgan2-chiral", "tanimoto", metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
            ("morgan2", "cosine", metrics.cosine, DataStructs.BulkCosineSimilarity))
    for sq in queries["similarity"]:
        q, src = sq["query"], uuid.UUID(sq["source_id"])
        qm = Chem.MolFromSmiles(q)
        for family, mname, metric, bulk in legs:
            scores = {i: s for i, s in zip(found, bulk(gens[family].GetFingerprint(qm), ref[family])) if s >= threshold}
            rec = check_similarity(sq["id"], vault.similar(q, threshold, family=family, metric=metric), scores, src, set())
            out["gate10"].append({"family": family, "metric": mname, **rec})
    for spec in FAMILIES:
        reference = {i: engine.pack_fingerprints(gens[spec.name], [Chem.MolFromSmiles(vault.get(i).smiles)], spec.nbytes)[0] for i in sample_ids}
        f, mat = vault.fingerprints(sample_ids, spec.name)
        out["gate10"].append({"family": spec.name, "metric": "rows", "id": "sample", **check_rows(f, mat, reference)})
```

`main`: build the vault with `families=FAMILIES`, pass `ids` as `all_ids`, save the snapshot after the run when a snapshot dir is set (`if a.snapshot_dir: vault.save_snapshot()`), and list failing ids from gates 2–10 in the final print.

- [ ] **Step 6: Run the unit tests**

Run: `uv run pytest tests/bench -q`
Expected: all pass.

- [ ] **Step 7: Commit the code**

```bash
git add bench/queries.py bench/parity.py tests/bench/test_queries.py tests/bench/test_parity.py
git commit -m "feat(bench): parity gates 5-10 for families, metrics, generalized, chiral and molblock queries

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/queries.py bench/parity.py tests/bench/test_queries.py tests/bench/test_parity.py
```

- [ ] **Step 8: Run the gates at 500k** (the loads from the milestone-2 run are reused; nothing is reloaded)

```bash
docker compose -f bench/compose.yml --profile molseal --profile cartridge up -d
STEPS="5" N=500000 bash bench/run_m2.sh        # regenerates bench/data/queries_500000.json with the chiral section
uv run python - <<'PY'
import json
new = json.load(open("bench/data/queries_500000.json")); old = json.load(open("docs/results/2026-10-13-500k/queries_500000.json"))
assert new["substructure"] == old["substructure"] and new["similarity"] == old["similarity"], "the published query sections moved"
print("chiral queries:", len(new["chiral"]))
PY
STEPS="6" N=500000 bash bench/run_m2.sh 2>&1 | tee bench/results/parity_500000_search_surface.log
```

Expected: the first vault open finds the format-1 snapshot, rebuilds from the database with three families (about four minutes) and saves a format-2 snapshot at the end; `parity OK`; gate 7 is the slow one (the cartridge's `@>>` scans every row, up to 30 s per query). Budget 45 minutes. If a gate fails, stop and report the failing records verbatim with `python -c "import json; ..."`; do not loosen a comparison.

- [ ] **Step 9: Publish the run**

```bash
mkdir -p docs/results/2026-10-14-500k-search-surface
cp bench/results/parity_500000.json bench/data/queries_500000.json docs/results/2026-10-14-500k-search-surface/
```

Write `docs/results/2026-10-14-500k-search-surface/README.md`:

```markdown
# 500k parity run for the search surface (2026-10-14)

Code: commit <sha of Step 7>. Data: the milestone-2 loads of the same 500k ChEMBL 37 slice on both sides
(`docs/results/2026-10-13-500k/`), reused; nothing was reloaded. The cartridge table gained an `fmfp`
column (feature-Morgan radius 2, 2048 bits) for gate 5. No timing legs were run.

| gate | what | queries | result |
|---|---|---|---|
| 1–4 | as in the milestone-2 run | 72 + 50 | <ok/fail> |
| 5 | feature-Morgan Tanimoto at 0.55 and 0.7 vs `featmorganbv_fp` | 50 × 2 | <ok/fail> |
| 6 | Tversky(1, 0) and Dice on Morgan vs `tversky_sml` / `dice_sml` | 50 × 2 | <ok/fail> |
| 7 | generalized substructure vs `@>> mol_to_xqmol` | 50 | <ok/fail> |
| 8 | chirality vs `rdkit.do_chiral_sss` | <n> | <ok/fail> |
| 9 | molblock queries equal their SMILES form | 50 | <ok/fail> |
| 10 | chiral Morgan and Cosine vs RDKit in Python; fingerprint rows for 100 sampled ids | 50 × 3 + 3 | <ok/fail> |

Wall time: <minutes>, of which gate 7 <minutes>. The first open rebuilt the index from the database
because the stored snapshot was format 1: <seconds>.

Query sets are CC BY-SA 3.0 derived from ChEMBL 37 (see the data attribution in the root README).
```

Fill every `<...>` from `parity_500000.json` and the log; no placeholders may remain. Add one line to `docs/results/README.md` pointing at the folder. Commit:

```bash
git add docs/results/2026-10-14-500k-search-surface docs/results/README.md
git commit -m "docs(results): 500k parity run for the search surface, gates 5-10

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- docs/results/2026-10-14-500k-search-surface docs/results/README.md
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`, `bench/README.md`, `docs/backlog/2026-10-13-generic-search-surface.md`
- Create: `docs/examples/registry-adapter.md`, `docs/backlog/2026-10-14-similarity-band-index.md`

- [ ] **Step 1: Add the search-surface sections to `README.md`** (insert after the "Quick start" command list, before "Absence check")

````markdown
## Search surface

Every search runs in the application process on an index built from the decrypted structures. The
database only ever holds ciphertext.

### Fingerprint families

A family is `FingerprintSpec(name, bits, generator)`: the name is its identity (change it when the
generator changes), bits is a multiple of 64, generator is a zero-argument factory returning any RDKit
fingerprint generator. Register families when you build the vault:

```python
from molseal.engine import MORGAN2, MORGAN2_CHIRAL, FEATMORGAN2
vault = Vault(store, keys, tenant, families=(MORGAN2, FEATMORGAN2))
```

| name | bits | generator | bytes per molecule |
|---|---|---|---|
| `morgan2` (default) | 2048 | Morgan radius 2 | 258 |
| `morgan2-chiral` | 2048 | Morgan radius 2 with chirality | 258 |
| `featmorgan2` | 2048 | feature-Morgan radius 2 | 258 |

A family registered later (`vault.index.add_family`) is computed from the structures already indexed;
nothing is read from the database. Each process declares the families it uses. A snapshot carries them
by name and recomputes a missing one on load.

### Similarity

```python
vault.similar(smiles, threshold=0.7, top_k=None, family="morgan2", metric=tanimoto)
```

A metric is a plain function of three counts, the intersection `c`, the query's bits `a` and the
target's bits `b`, in `molseal.metrics`. Any function with that shape works.

| metric | formula |
|---|---|
| `tanimoto` | c / (a + b − c) |
| `dice` | 2c / (a + b) |
| `cosine` | c / √(a·b) |
| `tversky(query_weight, target_weight)` | c / (α(a − c) + β(b − c) + c) |

Results are exact: every fingerprint is compared, there is no index and no approximation.

### Substructure

```python
vault.substructure(query, kind="smiles", max_results=None, generalized=False, use_chirality=False)
vault.count_substructure(query, kind="smiles", generalized=False, use_chirality=False)
```

| option | meaning |
|---|---|
| `kind` | `smiles`; `smarts`, taken literally with no aromaticity perception; `molblock`, sanitized with explicit hydrogens removed |
| `generalized` | tautomer-insensitive matching through RDKit's extended query mol, with the same defaults as the cartridge's `@>>` |
| `use_chirality` | defined stereo in the query must match defined stereo in the target, tetrahedral and E/Z |
| `max_results` | an arbitrary subset of the matches of that size; stable across runs only with one thread |

### Bulk reads

```python
payloads = vault.get_many(ids)                           # {id: Payload} for the live rows
found, rows = vault.fingerprints(ids, family="morgan2")   # packed uint8 rows, one per live id found
```

A packed row becomes an RDKit bit vector in two lines:

```python
bv = DataStructs.ExplicitBitVect(2048)
bv.SetBitsFromList(np.flatnonzero(np.unpackbits(row)).tolist())
```

Clustering, diverse picking, scaffold analysis and the like run in your own code on those rows and
payloads. The library does not implement them. Fingerprints are partially invertible: treat the rows
as confidential as the structures.

### Chemistry notes

1. Standardization is yours. The library canonicalizes and nothing more: salts, charges and tautomers
   are stored as given. Run a query through the same standardizer as the stored structures.
2. Similarity is tautomer-sensitive even though generalized substructure is not.
3. Tversky: `query_weight` (alpha) weights bits only in the query, `target_weight` (beta) bits only in
   the target. `tversky(1, 0)` scores how much of the query is inside the target.
4. Thresholds do not transfer across families or metrics: Dice runs higher than Tanimoto on the same
   pair, feature-Morgan higher than Morgan. A preset is a (family, metric, threshold) triple.
5. With chirality on, undefined stereo in the query matches anything and undefined stereo in the
   target never matches a defined query. The chiral family likewise needs stereo assigned on both sides.
6. SMARTS is literal: a Kekulé SMARTS does not match aromatic targets. Molblock queries drop explicit
   hydrogens, so a sketcher's hydrogens do not become required atoms.
7. All families are bit vectors. Count-based fingerprints are not supported.

Memory: 258 bytes per molecule per 2048-bit family on top of the substructure library. A snapshot of an
older format is ignored and the index is rebuilt from the database; the next save writes the current
format.
````

- [ ] **Step 2: Update the rest of `README.md`**

In "What is deliberately not built", remove "Tautomer-insensitive search" and "Stereo-aware search"; add "Count-based fingerprints" and "A similarity index (every query scans every fingerprint)". In "Results", add a line: `2026-10-14: parity-only run for the search surface, gates 5–10, in docs/results/2026-10-14-500k-search-surface/.` Point at `docs/examples/registry-adapter.md` from the end of the "Search surface" section with one sentence.

- [ ] **Step 3: Write `docs/examples/registry-adapter.md`**

````markdown
# Example: a compound registry on molseal

Generic sketch of how a registry application maps its own vocabulary onto molseal's primitives. The
library knows none of these names; they live in the application.

```python
from molseal import metrics
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
from molseal.vault import Vault

vault = Vault(store, key_provider, workspace_id, snapshot_dir=snapshots,
              families=(MORGAN2, MORGAN2_CHIRAL, FEATMORGAN2))

# Similarity presets are (family, metric, threshold) triples. Thresholds never transfer across triples.
PRESETS = {
    "similar":            ("morgan2",      metrics.tanimoto,      0.70),
    "scaffold-hop":       ("featmorgan2",  metrics.tanimoto,      0.55),
    "fragment-in-target": ("morgan2",      metrics.tversky(1, 0), 0.70),
    "same-stereo":        ("morgan2-chiral", metrics.tanimoto,    0.85),
}

def similar(mode: str, smiles: str, top_k: int | None = None):
    family, metric, threshold = PRESETS[mode]
    return vault.similar(standardize(smiles), threshold, top_k, family=family, metric=metric)
```

Standardize the query exactly as you standardize structures at registration (`standardize` is your
pipeline: salt stripping, charge neutralization, whatever you chose). The library stores what it is
given.

## Substructure from a sketcher

```python
def substructure(molblock: str, *, tautomers: bool = False, stereo: bool = False, limit: int | None = 1000):
    return vault.substructure(molblock, kind="molblock", max_results=limit,
                              generalized=tautomers, use_chirality=stereo)
```

A molblock query keeps a sketcher's drawing intact: explicit hydrogens are dropped and Kekulé rings are
aromatized on parse. A SMARTS query is taken literally.

## Registration and dedup through exact keys

```python
def register(molecule_id, smiles: str, inchikey: str, parent_inchikey: str):
    vault.put(molecule_id, smiles, {"inchikey": inchikey, "parent": parent_inchikey})

def already_registered(inchikey: str) -> list:
    return vault.find_exact("inchikey", inchikey)
```

Exact keys are HMACs keyed per tenant; the database never sees the InChIKey. Uniqueness is the
application's rule: keep it in your own table if two writers may race.

## Analytics on bulk reads

```python
from rdkit import DataStructs
from rdkit.ML.Cluster import Butina
import numpy as np

ids, rows = vault.fingerprints(candidate_ids, family="morgan2")
bvs = []
for row in rows:
    bv = DataStructs.ExplicitBitVect(2048)
    bv.SetBitsFromList(np.flatnonzero(np.unpackbits(row)).tolist())
    bvs.append(bv)
dists = [1 - s for i in range(1, len(bvs)) for s in DataStructs.BulkTanimotoSimilarity(bvs[i], bvs[:i])]
clusters = Butina.ClusterData(dists, len(bvs), 0.4, isDistData=True)

payloads = vault.get_many(ids)        # decrypted structures for scaffold or MCS work in your own code
```

Treat `rows` and `payloads` as confidential: they leave the vault only for this computation.
````

- [ ] **Step 4: Update the backlog and the bench README**

- `docs/backlog/2026-10-13-generic-search-surface.md`: change the status line to `**Status:** implemented in milestone 3; spec at docs/superpowers/specs/2026-10-14-generic-search-surface-design.md. The similarity band index is parked separately.`
- Create `docs/backlog/2026-10-14-similarity-band-index.md`:

```markdown
# Parked: similarity bit-count band index

**Status:** parked 2026-10-14 (user). Similarity is a full scan: 22 ms at 500k on one thread, growing
linearly. Acceptable for now.

**Design when picked up:** per family, a matrix kept sorted by bit count with an unsorted tail for new
rows, merged at a size threshold or at snapshot save. A threshold query scans only the contiguous range
of bit counts that can pass the metric's bound; a top-k query walks outward from the query's own count
and stops when no remaining band can beat the k-th score. Exact results, same metric functions.
Expected at 500k: threshold search 22 ms → about 8–10 ms, top-k from 26 ms → a few ms. Cost: a 4-byte
position map per molecule per family and the merge logic.

**Also parked from the same decision:** count-based fingerprints; the 1M and 2.4M rungs; the five bench
harness items from the milestone-2 ledger (statement-log positive control, `run_m2.sh` step-3 header
note, gate through a function instead of CLI stdout, GIT_SHA build argument, `_pack` profiling).
```

- `bench/README.md` section 6: after the sentence describing gates 1–4, add:

```markdown
Gates 5–10 (milestone 3) cover the rest of the search surface on the same loads: feature-Morgan
Tanimoto at 0.55 and 0.7 against `featmorganbv_fp` (the gate adds an `fmfp` column once); Tversky(1, 0)
and Dice on Morgan against `tversky_sml` and `dice_sml` with the query as the first argument;
generalized substructure against `m @>> mol_to_xqmol(...)`; chirality against `rdkit.do_chiral_sss`
on 20 seeded stereo molecules and their mirror images; molblock queries equal to their SMILES form; and,
for what the cartridge lacks, chiral Morgan and Cosine against RDKit in Python plus the fingerprint rows
of 100 sampled ids. `python -m bench.parity` runs all ten; a run with any gate failing publishes nothing.
```

- [ ] **Step 5: Check the docs for names and placeholders, then commit**

Run: `grep -rniE "TBD|TODO" README.md bench/README.md docs/examples docs/backlog/2026-10-1[34]-*.md`
Expected: no output. Then read each of those files once for any product, vendor or consumer-application name; the Global Constraints forbid them, so remove any you find.

```bash
git add README.md bench/README.md docs/examples/registry-adapter.md docs/backlog/2026-10-13-generic-search-surface.md docs/backlog/2026-10-14-similarity-band-index.md
git commit -m "docs: search surface, chemistry notes, registry adapter example, parked band index

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- README.md bench/README.md docs/examples/registry-adapter.md docs/backlog/2026-10-13-generic-search-surface.md docs/backlog/2026-10-14-similarity-band-index.md
```

- [ ] **Step 6: Full suite on the finished branch**

Run: `MOLSEAL_CARTRIDGE_URL=postgresql+psycopg://bench:bench@localhost:5440/bench MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal uv run pytest -q`
Expected: all pass, no skips.
