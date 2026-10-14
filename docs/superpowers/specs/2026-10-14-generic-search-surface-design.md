# Generic search surface — design (milestone 3)

**Status:** approved in conversation 2026-10-14; this file is the written spec for review.
**Supersedes:** `docs/backlog/2026-10-13-generic-search-surface.md` (the agreed direction), minus the similarity
band index, which is parked.
**Baseline:** `main` at tag `milestone-2` (engine, vault, snapshot format 1, bench with parity gates 1–4).

## 1. Goal

Make molseal cover the search types a compound registry needs so that a consumer can drop a chemistry
cartridge entirely: caller-registered fingerprint families, caller-chosen similarity metrics,
substructure options (chirality, tautomer-insensitive matching, molblock queries), and bulk reads of
decrypted structures and fingerprints for analyses the caller runs itself. Every behaviour is RDKit's
own. Where the cartridge has an equivalent, a parity gate at 500k proves the two agree; where it does
not, the gate compares against RDKit in Python.

## 2. Non-goals

- No similarity index (bit-count bands). Similarity stays a full scan; 22 ms at 500k is acceptable for now.
- No count-based fingerprints. All families are bit vectors; count fingerprints need a different scan.
- No consumer presets, mode names, thresholds or column names in the library. No product names anywhere.
- No analytics (scaffold trees, MCS, R-group decomposition, clustering, picking). The library hands out
  decrypted structures and bits; the caller's RDKit code does the rest.
- No standardization. The library canonicalizes (isomeric canonical SMILES) and nothing more.
- No new timing legs in the bench. No 1M or 2.4M runs.

## 3. Fingerprint families

### 3.1 Spec

```python
@dataclass(frozen=True)
class FingerprintSpec:
    name: str                              # identity; change it when the generator changes
    bits: int                              # > 0 and a multiple of 64
    generator: Callable[[], Any]           # zero-arg factory returning an RDKit fingerprint generator
```

Validation (ValueError): `name` matches `[a-z0-9][a-z0-9_-]*`; `bits % 64 == 0`; names unique within an
index. The generator must provide `GetFingerprints(mols, numThreads=n)` returning bit vectors of exactly
`bits` bits; the index checks the first vector's length on first use and raises ValueError otherwise.
Any RDKit `FingerprintGenerator` (Morgan, feature-Morgan, RDKit topological, atom pair, torsion) qualifies.

Shipped constants in `molseal.engine`:

```python
MORGAN2        = FingerprintSpec("morgan2",        2048, lambda: GetMorganGenerator(radius=2, fpSize=2048))
MORGAN2_CHIRAL = FingerprintSpec("morgan2-chiral", 2048, lambda: GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True))
FEATMORGAN2    = FingerprintSpec("featmorgan2",    2048, lambda: GetMorganGenerator(radius=2, fpSize=2048, atomInvariantsGenerator=GetMorganFeatureAtomInvGen()))
DEFAULT_FAMILIES = (MORGAN2,)
```

`MORGAN2` is bit-for-bit today's fingerprint (parity gate 1 keeps proving it against the cartridge).

### 3.2 Index storage

`StructureIndex(families: Sequence[FingerprintSpec] = DEFAULT_FAMILIES)`. Per family the index holds one
packed matrix `(cap, bits // 8) uint8` and one bit-count array `(cap,) uint16`, grown by the existing
capacity logic. `add_batch` computes every registered family for the parsed molecules, one
`GetFingerprints(mols, numThreads)` call per family. Packing keeps today's convention: bit *i* of the
vector is bit *i* of the row (`np.unpackbits(row)[i]`).

```python
StructureIndex.families -> tuple[FingerprintSpec, ...]
StructureIndex.add_family(spec, num_threads=-1) -> None   # computes the family for every position from the
                                                           # SMILES in the molecule holder; no-op if a family
                                                           # with the same name and bits exists; ValueError if
                                                           # the name exists with different bits
StructureIndex.drop_family(name) -> None                   # ValueError for the last remaining family
```

A family computed by `add_family` on a live index is bit-for-bit equal to the same family computed at
ingest (test). Dead positions are computed too; they are masked at search time as today.

### 3.3 Vault

`Vault(store, key_provider, tenant_id, snapshot_dir=None, num_threads=-1, refresh_batch=10_000,
families=DEFAULT_FAMILIES)`. The tuple is passed to every index the vault builds (fresh, rebuilt, or loaded
from a snapshot, see §7). Nothing about families is stored in Postgres; each process declares what it uses.

## 4. Metrics

A metric is a function of three things: `c` the per-row intersection counts (`ndarray[int]`), `a` the
query's bit count (`int`), `b` the per-row bit counts (`ndarray[int]`). It returns `ndarray[float64]`.

```python
Metric = Callable[[np.ndarray, int, np.ndarray], np.ndarray]

def tanimoto(c, a, b): return c / (a + b - c)
def dice(c, a, b):     return 2 * c / (a + b)
def cosine(c, a, b):   return c / np.sqrt(a * b)
def tversky(query_weight: float, target_weight: float) -> Metric:
    # query_weight (alpha) weights bits only in the query; target_weight (beta) bits only in the target.
    # tversky(1, 0) = c / a: "how much of the query is inside the target" (fragment-in-target).
    return lambda c, a, b: c / (query_weight * (a - c) + target_weight * (b - c) + c)
```

Every shipped metric returns 0.0 where its denominator is 0 (`np.divide(..., where=denom > 0)`).
A caller may pass any function with this shape. The shipped metrics live in `molseal.metrics`.

```python
StructureIndex.similar(query_smiles, threshold=0.7, top_k=None, *, family="morgan2", metric=tanimoto) -> list[tuple[UUID, float]]
Vault.similar(query_smiles, threshold=0.7, top_k=None, *, family="morgan2", metric=tanimoto)        -> list[tuple[UUID, float]]
```

Positional `threshold` and `top_k` keep today's call sites working. The query fingerprint comes from the
named family's generator. Unknown family → ValueError. The scan is today's: per 250k-row chunk compute
`c` with `np.bitwise_count(rows & q)`, call the metric, mask dead rows, filter `>= threshold`, apply
`top_k` by score, sort by score descending with stable ties. Results are exact.

## 5. Substructure options

```python
QueryKind = Literal["smiles", "smarts", "molblock"]

StructureIndex.substructure(query, kind="smiles", max_results=None, num_threads=-1, *,
                            generalized=False, use_chirality=False) -> list[UUID]
StructureIndex.count_substructure(query, kind="smiles", num_threads=-1, *,
                                  generalized=False, use_chirality=False) -> int
Vault.substructure(query, kind="smiles", max_results=None, *, generalized=False, use_chirality=False)
Vault.count_substructure(query, kind="smiles", *, generalized=False, use_chirality=False)
```

- **Parsing.** `smiles` → `MolFromSmiles`; `smarts` → `MolFromSmarts` (taken literally, no aromaticity
  perception); `molblock` → `MolFromMolBlock(query)` with its defaults (sanitize, hydrogens removed), so a
  sketcher block with explicit hydrogens and a Kekulé ring matches aromatic targets. Parse failure →
  `StructureError`.
- **generalized=True.** The parsed query is wrapped with
  `rdGeneralizedSubstruct.CreateExtendedQueryMol(q, doEnumeration=True, doTautomers=True, adjustQueryProperties=False)`,
  the cartridge's defaults, and handed to the library. If RDKit cannot build the extended query (some SMARTS),
  the call raises `StructureError` with RDKit's message; it never crashes the process.
- **use_chirality.** Passed straight to `GetMatches` / `CountMatches`. RDKit semantics: a query with defined
  stereo matches only targets with the same defined stereo (tetrahedral and E/Z); undefined stereo in the
  query matches anything; undefined stereo in the target does not match a defined query.
- **Pattern holder.** The index builds its screen with `AddPatterns(lib, TautomerPatternHolder(), numThreads)`
  instead of the plain holder. Measured on 20k molecules: same build time, identical hits for plain, SMARTS
  and generalized queries, generalized queries 4–5× faster because the screen is kept, library bytes +10%.
  Appends after the screen exists extend it, as today.
- **max_results.** Returns an arbitrary subset of the matches of that size. With `num_threads=1` the subset is
  the same across runs (test). With more threads it is unspecified and the docs say so.

## 6. Bulk reads

```python
Vault.get_many(ids: Iterable[UUID]) -> dict[UUID, Payload]
Vault.fingerprints(ids: Iterable[UUID], family: str = "morgan2") -> tuple[list[UUID], np.ndarray]
```

- `get_many` fetches in store batches of 10,000 (`store.get_structures`), decrypts in Python, and returns only
  live payloads: deleted and unknown ids are absent. No lock, like `get`.
- `fingerprints` takes the index lock, syncs exactly as a search does (read-your-writes), and returns the ids
  it found in the order given plus a packed `(len(found), bits // 8) uint8` copy with one row per found id.
  Deleted and unindexed ids are omitted. Unknown family → ValueError.
- The docs show the two lines that turn a packed row into an RDKit `ExplicitBitVect`
  (`np.unpackbits` then `SetBitsFromList`), so clustering and diverse picking run on the caller's side.

## 7. Snapshot format 2

- `FORMAT_VERSION = 2`; `MAGIC` unchanged. The version is already inside the authenticated header and the
  AAD prefix, so no new field is added.
- Blob layout: length-prefixed `meta`, then each part named in `meta["parts"]`, in that order, each
  length-prefixed. `meta` gains `"parts": ["lib", "ids", "live", "rv", "fp:<name>", ...]` and
  `"families": [{"name": ..., "bits": ...}, ...]`.
- `StructureIndex.to_parts()` emits one `fp:<name>` part per family. `StructureIndex.from_parts(parts, families)`
  builds every requested family whose stored name and bits match, drops stored families nobody requested, and
  calls `add_family` for requested families that are missing (computed from the SMILES in the library).
- `snapshot.load(path, data_key, tenant_id, key_version, families)` raises `SnapshotFormatError(SnapshotError)`
  when the header's format is not 2. `Vault._load_snapshot` catches only that error and returns None, so the
  vault rebuilds from the database (about two minutes at 500k) and overwrites the file on the next save. Wrong
  tenant, wrong key version, wrong key, truncation and tampering still raise as today.
- The bench's existing 500k snapshot is format 1; the first parity run after this change exercises the
  rebuild path for real and then saves a format-2 snapshot.

## 8. Parity gates (bench)

Gates 1–4 stay. New gates run at 500k on the loads already on disk (cartridge on :5440, molseal on :5441),
through `bench/parity.py`, written to `parity_500000.json`, and covered by `overall_ok`. Scores compare within
1e-6, hit sets exactly, under the existing 10,000-id comparison limit (larger sets record `skipped` as gate 3 does).

| gate | molseal side | reference | queries |
|---|---|---|---|
| 5 | `similar(q, t, family="featmorgan2")` at t = 0.55 and 0.7 | cartridge `tanimoto_sml(fmfp, featmorganbv_fp(mol_from_smiles(q), 2))`; the gate adds the `fmfp bfp` column once, lazily (`ADD COLUMN IF NOT EXISTS`, then `UPDATE ... WHERE fmfp IS NULL`) | the 50 similarity queries |
| 6 | `similar(q, 0.7, metric=tversky(1, 0))` and `metric=dice` on `morgan2` | cartridge `tversky_sml(morganbv_fp(mol_from_smiles(q)), mfp, 1.0, 0.0)` and `dice_sml(mfp, ...)` — query first, so the orientation is pinned | the 50 similarity queries |
| 7 | `substructure(q, generalized=True)` and `count_substructure(q, generalized=True)` | cartridge `m @>> mol_to_xqmol(mol_from_smiles(q))` (sequential scan; budget up to 30 s per query) | the 50 SMILES fragment queries |
| 8 | `substructure(q, use_chirality=True)` | cartridge `m @> mol_from_smiles(q)` on a connection with `SET rdkit.do_chiral_sss = true`; plus, on both sides, chirality-off hits ⊇ chirality-on hits | 20 seeded molecules from the slice with ≥ 1 defined stereocentre and ≤ 30 heavy atoms, each also as its mirror image (every `@@` ↔ `@`), 40 queries, generated by `bench/queries.py` |
| 9 | `substructure(MolToMolBlock(MolFromSmiles(q)), kind="molblock")` | molseal's own `substructure(q, kind="smiles")`, equal hit lists (self-consistency) | the 50 SMILES fragment queries |
| 10 | `similar` on `morgan2-chiral` (Tanimoto) and on `morgan2` with `cosine`, at 0.7; and `fingerprints(sample_ids, f)` for every family | RDKit in Python: fingerprints computed from `get_many` over all live ids in 10,000-id batches, `BulkTanimotoSimilarity` / `BulkCosineSimilarity`; `morgan2` Tanimoto runs through the same path as a control; sampled rows must equal the Python-packed bytes | the 50 similarity queries; the gate-1 sample ids |

The comparison helpers stay pure functions with unit tests; one test proves that a swapped Tversky pair fails
gate 6 against fixture values. `bench/run_m2.sh` step 6 (`STEPS="6"`) runs the whole gate set; the bench README
lists gates 5–10. Running gate 10 costs about three minutes (500k decrypts plus two fingerprint passes).

## 9. Documentation

- `README.md` gains a **Search surface** section: a families table (name, bits, generator, bytes per
  molecule), a metrics table with formulas, and an options table for substructure; a **Chemistry notes**
  section with the six points below plus the bit-vector-only limitation; memory per family (258 bytes per
  molecule at 2048 bits) and the format-2 rebuild behaviour.
  1. Standardization is the caller's job, and a query must go through the same standardizer as the stored
     structure (salts, charges, tautomers are not normalized by the library).
  2. Similarity is tautomer-sensitive even though generalized substructure is not.
  3. Tversky's `query_weight` is alpha, `target_weight` is beta; `tversky(1, 0)` is fragment-in-target.
  4. Thresholds do not transfer across families or metrics; a preset is a (family, metric, threshold) triple.
  5. Chirality-on semantics, as in §5, for both substructure and the chiral family.
  6. SMARTS is literal (a Kekulé SMARTS does not match aromatic targets); molblock queries drop explicit hydrogens.
- `docs/examples/registry-adapter.md`: a generic registry adapter with three similarity presets as triples,
  generalized substructure from a sketcher molblock, a chirality toggle, dedup through an exact key, a
  clustering snippet over `fingerprints`, and the standardize-both-sides rule. Generic wording only.
- `docs/backlog/2026-10-13-generic-search-surface.md`: status line updated to point at this spec; the band
  index moved to its own parked entry.

## 10. Tests

- `tests/test_engine.py`: family validation (bad name, bits not a multiple of 64, duplicate name, generator
  of the wrong width); `add_family` on a live index equals ingest-time computation bit-for-bit; `drop_family`
  refuses the last family; `similar` with each shipped family and metric against RDKit `DataStructs` values
  on the same molecules; unknown family raises; the tautomer pair found only with `generalized=True`;
  enantiomers and an undefined-stereo target under `use_chirality`; a sketcher-style molblock with explicit
  hydrogens and a Kekulé benzene ring matching an aromatic target; `generalized=True` with a SMARTS either
  matches or raises `StructureError`; capped single-thread determinism; `fingerprints` alignment, omission of
  dead and unindexed ids.
- `tests/test_metrics.py`: the four metrics against RDKit `TanimotoSimilarity`, `DiceSimilarity`,
  `CosineSimilarity`, `TverskySimilarity` on random bit vectors, including the swapped-weights case; zero
  denominators.
- `tests/test_snapshot.py`: format-2 round trip with two families; a requested family missing from the file
  is recomputed and equals a fresh index; a stored family not requested is absent after load; a format-1 file
  raises `SnapshotFormatError`; tampering still raises `SnapshotError`.
- `tests/test_vault.py`: `get_many` across a delete made by a second vault instance on the same store;
  `fingerprints` sees a put made by a second instance (sync before read); a vault with a format-1 snapshot
  on disk rebuilds instead of raising and saves format 2.
- `tests/bench/test_parity.py` and `test_queries.py`: pure comparison helpers for gates 5–10, the swapped
  Tversky case, chiral query generation (count, stereocentre present, mirror image differs and re-parses).

## 11. Memory and time expectations (stated in the docs, checked in the parity run)

- Each 2048-bit family: 258 bytes per molecule; three families at 500k ≈ 390 MB, at 2.4M ≈ 1.9 GB on top of
  the ~5.5 GB estimate.
- Library bytes +10% for the tautomer pattern holder.
- Ingest: one extra generator pass per extra family (Morgan at 500k is seconds, multi-threaded).
- Rebuild from the database at 500k with three families: about three to four minutes; snapshot load stays
  under two seconds.

## 12. Global constraints

- The library stays generic: no consumer vocabulary, no product names, tenants opaque, exact keys caller-defined.
- Never commit `*.key`, `bench/data`, `bench/results`, `talks/`. ChEMBL is never redistributed.
- RDKit pinned at 2025.03.3 on both sides; numpy ≥ 2.0 (`bitwise_count`); Python ≥ 3.12.
- Every new behaviour has a test; every behaviour with a cartridge equivalent has a parity gate.

## 13. Parked (recorded here so they are not lost)

Similarity bit-count band index; count-based fingerprints; the 1M and 2.4M rungs; the five bench-harness
items from the milestone-2 ledger (statement-log positive control, `run_m2.sh` step-3 header note, gate through
a function instead of CLI stdout, GIT_SHA build argument, `_pack` profiling).
