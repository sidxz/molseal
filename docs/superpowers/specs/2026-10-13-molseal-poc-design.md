# molseal POC — design

Date: 2026-10-13
Status: approved design, pre-implementation

## 1. What this is

molseal is a small, generic Python library that keeps chemical structures encrypted at the
application level inside any SQL database, and performs substructure, similarity and exact-match
search in-process with RDKit. The database only ever holds ciphertext. The search index lives in
the memory of the trusted application process.

This is **encrypted blob storage plus a plaintext in-memory index on a trusted host**. It is not
"encrypted search". Anyone with the application's memory, swap or core dumps sees structures.
Anyone with only database access does not.

The POC proves three things:

1. **Absence.** The database files, an uncompressed dump and full statement logs contain no
   loaded structure, and the residual leakage (size class, equality of exact keys) is measured
   and published rather than hand-waved.
2. **Parity.** On the same data, same RDKit release and pinned match flags, results are identical
   to the RDKit PostgreSQL cartridge, and latency is competitive with a properly indexed cartridge.
3. **Generality.** The package has no dependency on any consumer. It runs on stock PostgreSQL
   and on SQLite.

### 1.1 Generic-library rule (hard constraint)

The library is for anyone. Nothing in `src/molseal` may reference ChemCellar or any other
consumer, nor any consumer concept (workspace, registration number, project). Concretely:

- Tenants are opaque UUIDs. The library does not know what a tenant is.
- Exact-match keys are caller-defined `(kind, value)` strings. The library never computes
  InChIKeys or decides what "the same structure" means.
- The key provider and the store are protocols. The POC ships one implementation of each
  (file-based key, SQLAlchemy Core store). KMS providers and other stores are the consumer's or
  a later contributor's job.
- Consumer integration (for ChemCellar: the molecule repository adapter, workspace-to-tenant
  mapping, Infisical key provider, Temporal codec) lives in the consumer's repository, layered
  on top. None of it is in this repo.

## 2. Threat model

**Attacker:** has full read access to the database (credentials, data files, WAL, backups,
replicas, statement logs) and may also have write access to it.

**Trusted:** the application process running molseal, and the key provider. The key-encryption
key must live outside the database host, its volumes and its backup set. In the POC it is a file
whose path comes from an environment variable; in production it is a KMS or secrets manager.

**Guaranteed against a read attacker:** no structure, no fingerprint, no exact-key value is
recoverable from anything the database holds.

**Leaks accepted and measured:**

- Row count, tenant id, timestamps, change sequence.
- Ciphertext **size class** (power-of-two classes from 256 bytes). The POC publishes the
  measured correlation between size class and heavy-atom count. Molfiles are excluded from the
  searchable row for this reason; a 32-byte-bucketed molfile leaks atom and bond counts almost
  exactly (reviewer measured r = 0.999).
- **Equality of exact keys within a tenant.** The HMAC index reveals that two rows share an
  exact key (for example a deleted-then-re-registered structure). It does not allow offline
  guess-and-confirm against public InChIKey lists, which plain InChIKeys would.

**Against a write attacker:** ciphertexts cannot be moved between rows, tenants or key versions
(associated data binds them), wrapped tenant keys cannot be swapped between tenants, tombstones
and exact keys cannot be forged (they are inside the authenticated payload), and tampered
ciphertext or snapshots are rejected. **Not detectable** without a trusted root: rollback of a
row to an earlier valid version, deletion of rows, and restoring an older backup. The engine
detects sequence regression and rebuilds, but cannot prove which state is correct. Stated in the
README as an explicit non-goal.

## 3. Architecture

```
src/molseal/
  keys.py      KeyProvider protocol; FileKeyProvider; wrap/unwrap of tenant keys
  crypto.py    AES-GCM seal/open with AAD; size-class padding; chunked seal/open for big blobs
  payload.py   Payload dataclass <-> canonical JSON bytes
  store.py     StructureStore protocol; SqlStore (SQLAlchemy Core; PostgreSQL + SQLite)
  engine.py    StructureIndex: SubstructLibrary + Morgan matrix + dead mask; search API
  snapshot.py  encrypted, chunked on-disk snapshot of a StructureIndex
  vault.py     Vault facade per tenant: put/delete/get/find_exact/search/refresh/rebuild
  cli.py       argparse CLI: keygen, load, search, count, similar, snapshot, absence-check, leak-report
bench/         ChEMBL loader, cartridge loader, query set, runner, compose, bench Dockerfile
tests/         unit (no DB), store (SQLite), integration (PostgreSQL, skipped without URL)
docs/superpowers/{specs,plans}/
```

No chemistry in `store.py`. No SQL in `engine.py`. `vault.py` is the only module that imports
all of them.

## 4. Data model

Four tables, all keyed by tenant first.

```
tenants          (tenant_id uuid PK, seq_cursor bigint, live_count bigint, data_key_version int, hmac_key_version int)
tenant_keys      (tenant_id, kind text in ('data','hmac'), version int, nonce bytea, wrapped bytea,
                  PK (tenant_id, kind, version))
structures       (tenant_id, id uuid, row_version int, data_key_version int, nonce bytea,
                  ciphertext bytea, deleted bool, seq bigint, updated_at timestamptz,
                  PK (tenant_id, id), INDEX (tenant_id, seq))
structure_keys   (tenant_id, id, kind text, hmac bytea,
                  PK (tenant_id, id, kind), INDEX (tenant_id, kind, hmac))
```

- `id` is caller-provided, so a consumer's aggregate id and its vault row share an identity.
- `row_version` starts at 1 and increments on every write to the row, including delete.
- `deleted` and `structure_keys` are **indexes only**. The authoritative tombstone and exact keys
  are inside the encrypted payload. Readers verify against the payload.
- `seq` is assigned from `tenants.seq_cursor` in the writer's own transaction
  (`UPDATE tenants SET seq_cursor = seq_cursor + 1 ... RETURNING seq_cursor`). The row lock is
  held to commit, so sequence order equals commit order. This closes the classic hazard where a
  PostgreSQL sequence value is assigned at insert but committed later and skipped by a reader.
  Works unchanged on SQLite.

### 4.1 Payload

```json
{"v": 1, "smiles": "<RDKit canonical SMILES>", "keys": {"inchikey": "..."}, "deleted": false}
```

Serialized as canonical JSON (sorted keys, no whitespace) then padded (see 5.3). `smiles` is
always the RDKit canonical SMILES produced by this library on ingest, which is what makes the
trusted-SMILES holder valid (see 6.2). Molfiles are out of scope for the POC.

## 5. Cryptography

All primitives from the `cryptography` package. No custom constructions.

### 5.1 Key hierarchy

- **KEK** (32 bytes) from `KeyProvider.kek()`. `FileKeyProvider` reads a file and refuses it
  unless its mode is 0600.
- Per tenant, two independent 32-byte secrets, each with its own version:
  - **data key**: encrypts structure payloads and snapshots.
  - **hmac key**: keys the exact-key HMACs. Independent so that rotating the data key does not
    change every exact key.
- Tenant keys are wrapped with AES-GCM under the KEK, with associated data
  `tenant_id || kind || version`. A wrapped key that fails to open under its claimed tenant and
  version is rejected.
- Rotation is **out of scope** to implement. The versions exist so it can be added without a
  schema change. The POC always uses version 1.

### 5.2 Row encryption

- AES-256-GCM. Nonce: 12 bytes from `os.urandom` on **every** encryption, never derived from id or
  version. A unit test asserts a fresh nonce on update.
- Associated data: `tenant_id || id || data_key_version || row_version` (fixed-width encoding).
- Lifetime budget under one data key is about 2^32 encryptions (NIST bound for random 96-bit
  nonces). Stated in the README; the version columns are the rotation path.
- The exact-key HMAC is `HMAC-SHA256(hmac_key, kind || 0x00 || value)`. Lookups return candidate
  ids; the facade decrypts each candidate and confirms the key is present in its payload before
  returning it.

### 5.3 Padding

Serialized payload is padded to the smallest power-of-two size class ≥ 256 bytes (256, 512,
1024, ...). Padding format: payload bytes, then zero bytes, with the true length encoded in the
last 4 bytes. The `leak-report` command measures what the size class still reveals.

### 5.4 Snapshot encryption

The `cryptography` AEAD interface refuses inputs over 2^31 − 1 bytes, and a full-ChEMBL snapshot
is about 1.6 GB. Snapshots are therefore encrypted in 64 MiB chunks. Each chunk's associated data
is `tenant_id || data_key_version || format_version || chunk_index || chunk_count || last_seq`.
A snapshot with a missing, reordered or tampered chunk fails to open.

## 6. Engine

One `StructureIndex` per tenant.

### 6.1 Contents

- `rdkit.Chem.rdSubstructLibrary.SubstructLibrary(CachedTrustedSmilesMolHolder, PatternHolder(2048))`.
- `fps`: numpy `uint8` matrix of shape (capacity, 256) holding Morgan radius-2, 2048-bit
  fingerprints (default invariants: no chirality, bond types on, ring membership on, which matches
  the cartridge's `morganbv_fp`). Grown by doubling capacity.
- `ids`: list of UUIDs by position. `pos_of`: dict id → latest live position.
- `live`: boolean mask by position. `row_versions`: dict id → row_version seen.
- `last_seq`: highest sequence ingested.

Memory estimate (reviewer measured on drug-like molecules, ×1.3 for ChEMBL's longer tail): about
0.7–0.8 KB per molecule, so full ChEMBL (~2.4M) is roughly 1.6–2.0 GB resident, plus transient
copies during snapshot. These are predictions the benchmark confirms or corrects.

### 6.2 Ingest pipeline

`append_batch(records)` for a chunk of decrypted records (chunks of ~50k):

1. Parse SMILES **with full sanitization** via `MultithreadedSmilesMolSupplier` (or a chunked
   process pool), drop unparseable rows with a logged count.
2. Canonicalize with `MolToSmiles`. This canonical SMILES goes into the trusted holder and into
   the stored payload, so the holder's assumptions (RDKit aromaticity model, RDKit canonical
   output) always hold. Any other-toolkit SMILES a caller passes is re-canonicalized on ingest.
3. Morgan fingerprints via `GetMorganGenerator(radius=2, fpSize=2048).GetFingerprints(mols, numThreads=-1)`
   into `fps`.
4. Append to the library. Pattern fingerprints: on the **initial build** add all molecules first,
   then `AddPatterns(library, numThreads=N)` (parallel). After the library has a fingerprint
   holder, `AddPatterns` refuses to run again, so **incremental appends use serial `AddMol`**,
   which computes the pattern fingerprint per molecule (~0.1 ms each). Documented limitation.
5. Discard the Mol objects. Never materialize the whole dataset as Mols.

### 6.3 Edits, deletes and the dead mask

SubstructLibrary cannot delete. The sanctioned mechanism is `SetSearchOrder(list_of_live_positions)`.

- An edit or delete marks the old position dead in `live`, updates `pos_of`, and (for an edit)
  appends the new structure at a new position.
- `AddMol` does **not** extend an existing search order (reviewer verified), so after every batch
  append the search order is reset to the current live positions. Dead positions are excluded
  **before** matching, which keeps result caps honest and makes duplicates impossible in
  substructure results. Similarity masks dead positions before top-k selection.
- Results are deduplicated by id as a belt-and-braces step.

### 6.4 Search API and match flags

- `substructure(query, kind="smiles"|"smarts", max_results=None, num_threads=-1) -> list[UUID]`
- `count_substructure(query, kind, num_threads=-1) -> int`
- `similar(query_smiles, threshold=0.7, top_k=None) -> list[tuple[UUID, float]]`
- Match flags are **fixed**: `useChirality=False`, `recursionPossible=True`,
  `useQueryQueryMatches=False`. This matches the cartridge default `rdkit.do_chiral_sss = off`.
  The trusted holder parses without sanitization and loses double-bond stereo, so stereo-aware
  search would require `CachedSmilesMolHolder`; it is out of scope and documented.
- `max_results=None` means uncapped (`maxResults=-1`); RDKit's default of 1000 is never relied on
  implicitly.
- Tanimoto: `(X & q).popcount / (X.popcount + q.popcount − and.popcount)` over a `uint64` view of
  `fps`, scanned in chunks of ~250k rows to bound temporaries. Optional popcount-bounds prefilter
  (Swamidass–Baldi) if the benchmark shows it matters.

### 6.5 Locking and consistency

- One `threading.Lock` serializes writes and reads in the POC. A readers–writer lock is the
  upgrade when concurrent readers matter (marked in code).
- **Read-your-writes, single process:** `Vault.put` appends to the engine synchronously after the
  store commit. Every search first reads `tenants.seq_cursor`; if it moved past `last_seq`, the
  vault calls `refresh()` before searching.
- `refresh()` pulls `changes_since(last_seq)` in batches and applies 6.2/6.3. It also compares the
  store's max sequence and live count with the engine's; on regression (sequence went backwards,
  or live count disagrees) it triggers `rebuild()`.
- `rebuild()` builds a fresh index aside and swaps it in under the lock. Expected cost at full
  ChEMBL: about a minute parallel.
- Multi-process deployments hold one index per process and get consistency only through the
  cursor check. Multi-process invalidation is out of scope and stated.

### 6.6 Snapshot

`Serialize()` of the library (bytes API; the stream API fails on binary data in 2025.03) plus
`ids`, `live`, `row_versions`, `fps[:n]` and `last_seq`, packed and encrypted per 5.4 to a file
under a configured directory. Loading restores the index and then calls `refresh()` to catch up.
Expected serialize time at full ChEMBL: under ten seconds.

## 7. Store protocol

```python
class StructureStore(Protocol):
    def ensure_tenant(self, tenant_id) -> TenantRow
    def tenant_state(self, tenant_id) -> tuple[int, int]            # (seq_cursor, live_count), one statement
    def get_tenant_keys(self, tenant_id) -> list[WrappedKey]
    def put_tenant_keys(self, tenant_id, keys: list[WrappedKey]) -> None   # one transaction
    def put_structure(self, rec, key_hmacs, expected_row_version) -> int   # compare-and-set; raises VersionConflict
    def put_structures(self, batch) -> list[int]                     # milestone 2: one transaction, cursor += len(batch)
    def get_structure(self, tenant_id, id) -> StructureRecord | None
    def changes_since(self, tenant_id, seq, limit, upto=None) -> list[StructureRecord]
    def max_seq(self, tenant_id) -> int
    def find_by_key(self, tenant_id, kind, hmac) -> list[UUID]    # live rows only
```

`SqlStore` implements it with SQLAlchemy Core and runs its test suite on SQLite and PostgreSQL.
`put_structure` is one transaction: bump the cursor (taking the tenant row lock), compare the row's
current `row_version` with the expected one and raise `VersionConflict` on mismatch, upsert the row,
replace its key rows, and adjust `tenants.live_count` by the row's live-state transition. The vault
retries a conflicting write up to five times.

**Bulk ingestion (milestone 2, agreed 2026-10-13).** `put_structures(batch)` does the same for a whole
batch in one transaction: the cursor advances by the batch size so sequence numbers stay consecutive
and commit-ordered, rows and key rows are bulk-inserted, compare-and-set still applies per row. The
vault exposes `put_many(items, chunk=10_000)`, which canonicalizes and encrypts a chunk in Python,
calls `put_structures`, and appends the chunk to the index once. The ChEMBL loader is its first
consumer. The library stays synchronous: its work is CPU-bound in RDKit and the cipher, so consumers
call `put_many` from a worker thread or a workflow activity; no async API is planned.

## 8. Benchmark

### 8.1 Data

ChEMBL 37 `chembl_37_chemreps.txt.gz` (CC BY-SA 3.0, attributed in the README, never
redistributed). The loader canonicalizes with RDKit in a process pool, computes InChIKey as the
example exact key, and writes a slice of N rows to a local file ignored by git.

Ladder: **N = 500k first**, then 1M, then full (~2.4M). Each run records its N.

The loader is resumable: it writes the slice to a local file once, loads it through `put_many` in
chunks of 10k, and records the last completed chunk in a progress file next to the slice, so a run
that dies at chunk 180 resumes at 180. Before the first benchmark the loader runs a 10k slice and the
absence check (section 9) must pass on it, including the dump leg; that is milestone 2's first gate.

### 8.2 Containers and sizing

- `molseal-db`: `postgres:16` with `log_statement=all` during the absence check.
- `cartridge-db`: `informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3` (arm64 manifest
  available; architecture printed in the report). Settings: `shared_buffers=4GB`,
  `maintenance_work_mem=2GB`, `effective_cache_size=8GB`, `work_mem=256MB`,
  `rdkit.morgan_fp_size=2048`, `rdkit.do_chiral_sss=off`. Named volume, not a bind mount.
- Docker Desktop VM raised to at least 16 GB (24 GB for the full run). Only one database runs at a
  time (compose profiles).
- Cartridge schema: `mols(id, m mol, mfp bfp)` with `GiST(m)` and `GiST(mfp)`, plus a
  `smiles text` column for the "consumer's current path" query.
- For the head-to-head, molseal runs **inside a container on the same VM** (`bench/Dockerfile`).
  Host-native molseal numbers are reported separately and labeled.
- RDKit release pinned identically: PyPI `rdkit==2025.3.3`, cartridge `Release_2025_03_3`. The
  write-up uses the form "2025.03.3" throughout and notes the PyPI spelling once.

### 8.3 Query set (deterministic, seed published)

- Substructure, ~50 queries: BRICS fragments of 5–20 heavy atoms cut from seeded random ChEMBL
  molecules (varied selectivity), plus ~20 SMARTS from a functional-group list including ring
  queries (`[R]`, `[r5]`, `[x3]`, ring-bond primitives).
- Similarity, 50 queries: seeded random ChEMBL molecules. The query molecule itself is excluded
  from results on both sides.
- The query list with per-query hit counts is published with the results.

### 8.4 Parity contract

Run before any timing is trusted.

1. **Fingerprint parity:** for 100 sampled molecules, the cartridge's `morganbv_fp(m)` bytes
   (`bfp_to_binary_text`) equal the Python generator's bits.
2. **Substructure count parity:** for every query, `count_substructure` (chirality off) equals
   `SELECT count(*) FROM mols WHERE m @> q::qmol`.
3. **Substructure id-set parity:** for every query with count ≤ 10,000, the uncapped id sets are
   identical.
4. **Similarity parity:** with `rdkit.tanimoto_threshold = 0.7`, the id set from `mfp % qfp`
   equals molseal's `similar(threshold=0.7)`, and scores agree to 1e-6.

Any divergence is a bug or a documented semantic difference; the run is not reported otherwise.

### 8.5 Timings

- Each query: one warm-up, then ≥ 5 repeats; per-query median; p50 and p95 across queries.
- molseal: `num_threads=1` and `num_threads=-1` reported separately, since a PostgreSQL backend is
  single-threaded.
- Capped runs: `GetMatches(maxResults=1000)` vs `... LIMIT 1000`, compared on count only.
- Cartridge: indexed `m @> q` (fair baseline); unindexed `mol_from_smiles(smiles) @> q` labeled
  **"consumer's current path"**, not "baseline".
- Scope labels: "search-only" for both sides; plus "end-to-end" for molseal, which includes the
  cursor check and a `WHERE id = ANY(:ids)` fetch of the hit rows.
- Cold vs warm for both sides: cold = after container restart; warm = after `pg_prewarm` on table
  and indexes (cartridge) or after the first pass (molseal).
- Also recorded: load/build time per side (labeled non-comparable: parallel native vs
  single-threaded in the VM), resident memory (`ru_maxrss`), snapshot size, cold load time from
  snapshot, GiST build time.

Output: a JSON file per run and a Markdown table; the write-up copies the table.

## 9. Absence check and leakage report

`molseal absence-check` loads the plaintext slice and scans, for every SMILES and exact-key value:

1. The concatenated bytes of every `ciphertext`, `nonce`, `hmac` and `wrapped` column.
2. `pg_dump -Fp --compress=0` output (plain, uncompressed; the default custom format is
   compressed and would hide even plaintext).
3. The PostgreSQL log captured with `log_statement=all` during the load (encryption is
   client-side, so parameters in statement logs are ciphertext).

Expected: 0 hits. **Negative control:** the command also inserts one deliberately plaintext
control row and confirms the scanner finds it, so "0 hits" is falsifiable.

`molseal leak-report` computes, over the loaded slice, the correlation between ciphertext size
class and heavy-atom count and the accuracy of predicting a heavy-atom-count bucket from the size
class. Both numbers go in the README verbatim.

## 10. Tests

- **crypto:** seal/open round trip; AAD tamper rejected; wrong tenant/version rejected; padding
  round trip at every class boundary; fresh nonce on update; wrapped-key AAD tamper rejected;
  chunked snapshot: missing, reordered and tampered chunk rejected.
- **engine (no DB):** substructure and similarity results equal brute-force RDKit on 1k ChEMBL
  molecules with chirality off, including ring SMARTS; dead mask: after an edit, a query matching
  only the old structure returns nothing for that id and no duplicates appear; results after a
  batch append are visible (search-order reset).
- **store (SQLite):** put/get/changes_since ordering; find_by_key tenant-scoped and live-only;
  cursor increments per write including delete.
- **integration (PostgreSQL, skipped without `MOLSEAL_TEST_DATABASE_URL`):** two concurrent
  writers where the first to take a sequence commits last; the engine ingests both. Rollback
  simulation (rows deleted and cursor reset) triggers rebuild.
- **vault:** read-your-writes; find_exact verifies against payload (a copied HMAC row does not
  return the wrong id); snapshot save/load/refresh round trip.

## 11. Dependencies and versions

Python 3.13, `uv`. `rdkit==2025.3.3`, `cryptography`, `numpy>=2.0` (`bitwise_count`),
`sqlalchemy>=2.0`, `psycopg[binary]>=3`. Dev: `pytest`. Stdlib for CLI (`argparse`), JSON, HMAC
inputs, resource usage. No other runtime dependencies.

License BSD-3-Clause, matching RDKit. Before the repository goes public, check the university's
policy on open-sourcing work done on its time.

## 12. Write-up framing

- Say what it is: encrypted-at-rest blob store plus a plaintext in-memory RDKit index in a trusted
  process. Not encrypted search.
- Do not frame the cartridge as deficient. It was never a cryptographic boundary. The point is
  that app-level encryption moves search out of the database, and that this is viable at
  registration-system scale on stock PostgreSQL or managed Postgres without the extension.
- Publish the parity contract results, the leakage numbers, the thread-count-separated timings,
  the query list, and the VM/CPU/memory configuration. Never show a multi-threaded molseal number
  next to a single-backend cartridge number without the label.

## 13. Out of scope (documented, not built)

Key rotation; tautomer-insensitive search; stereo-aware search; molfile storage; multi-process
invalidation; async API (the library is synchronous by design, see section 7); KMS key providers;
any consumer adapter; readers–writer lock; schema migrations (the POC creates tables with
`create_all`; an existing database gets new columns by hand).

## 14. Milestones (detail in the implementation plans)

1. **Done 2026-10-13, merged to main, tag `milestone-1`.** Library with unit, store and vault tests
   green (101 tests with PostgreSQL); absence check with negative control passing on the 438-molecule
   offline set with a real dump. The final review's findings (hex scanning in the absence check,
   compare-and-set on writes, transactional live count, lazy search order, safe key bootstrap,
   re-parse check on canonical SMILES) are fixed and recorded in `docs/superpowers/reviews/`.
2. Benchmark at N = 500k: 10k absence run first, then `put_many` and the resumable loader, the
   cartridge container, the query set, parity contract green, timings and leakage report produced.
3. N = 1M.
4. Full ChEMBL; write-up draft.
