# molseal

Encrypted chemical structures in any SQL database, with RDKit search done in-process.

Status: Proof of concept, milestone 1: library, tests and absence check. Benchmarks against the RDKit cartridge are the next milestone.

molseal keeps chemical structures encrypted at the application level inside any SQL database, and runs substructure, similarity and exact-match search in-process with RDKit. This is encrypted blob storage plus a plaintext in-memory index in a trusted process. It is not encrypted search. Anyone with the application's memory, swap or core dumps sees structures. Anyone with only database access does not. The database only ever holds ciphertext.

Design: `docs/superpowers/specs/2026-10-13-molseal-poc-design.md`.

## Threat model

**Attacker.** Has full read access to the database: credentials, data files, WAL, backups, replicas and statement logs. May also have write access.

**Trusted.** The application process running molseal, and the key provider.

**Guaranteed against a read attacker.** No structure, no fingerprint and no exact-key value is recoverable from anything the database holds.

**Leaks accepted and measured.**

- Row count, tenant id, timestamps, change sequence.
- Ciphertext size class (power-of-two classes from 256 bytes). Size class correlates with heavy-atom count. Molfiles are excluded from the searchable row for this reason.
- Equality of exact keys within a tenant. The HMAC index shows that two rows share an exact key. It does not allow offline guess-and-confirm against public InChIKey lists.

**Against a write attacker.** Ciphertexts cannot be moved between rows, tenants or key versions. Wrapped tenant keys cannot be swapped between tenants. Tombstones and exact keys cannot be forged. Tampered ciphertext or snapshots are rejected.

**Not detectable** without a trusted root: rollback of a row to an earlier valid version, deletion of rows, and restoring an older backup. The engine detects sequence regression and rebuilds, but it cannot prove which state is correct. This is an explicit non-goal.

## Keys

The key-encryption key must live outside the database host, its volumes and its backup set. In this POC it is a file whose path comes from `--key` or `MOLSEAL_KEY_FILE`. In production it should be a KMS or secrets manager.

Rows use AES-256-GCM with a random 96-bit nonce on every encryption. The lifetime budget under one data key is about 2^32 encryptions (the NIST bound for random 96-bit nonces). Key versions are stored with every row and are the path to rotation. Rotation is not implemented.

## Quick start

```bash
uv sync
docker compose up -d db        # PostgreSQL on port 5439

export MOLSEAL_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal
export MOLSEAL_KEY_FILE=/tmp/molseal.key
export MOLSEAL_TENANT=$(python -c "import uuid; print(uuid.uuid4())")
export MOLSEAL_SNAPSHOT_DIR=/tmp/molseal-snap

uv run python -c "import tests.mols as m; print('\n'.join(m.molecules()))" > /tmp/dev.smi

uv run molseal keygen $MOLSEAL_KEY_FILE
uv run molseal load /tmp/dev.smi                 # writes /tmp/dev.smi.ids.tsv
uv run molseal search 'c1ccccc1O'
uv run molseal count 'c1ccccc1O'
uv run molseal similar 'CC(=O)Oc1ccccc1C(=O)O' --threshold 0.5 --top 10
```

Commands:

- `molseal keygen PATH`
- `molseal load FILE` (one SMILES per line)
- `molseal search QUERY [--smarts] [--max N]`
- `molseal count QUERY`
- `molseal similar SMILES [--threshold T] [--top K]`
- `molseal absence-check SIDECAR [--run-pg-dump] [--dump PATH] [--log PATH]`
- `molseal leak-report SIDECAR`

Each command takes `--db`, `--key`, `--tenant` and `--snapshot-dir`, or the environment variables `MOLSEAL_DATABASE_URL`, `MOLSEAL_KEY_FILE`, `MOLSEAL_TENANT` and `MOLSEAL_SNAPSHOT_DIR`.

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

Declare every family in `Vault(..., families=)`; that tuple is what every rebuild and every snapshot
load uses. To add a family to a populated tenant, construct the vault with the new tuple: the first load
computes the new family from the structures already indexed, with no database read. A snapshot carries
families by name and recomputes a missing one on load.

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
   RDKit's trusted SMILES holder never assigns double-bond stereo, so a chirality-on query with a defined
   double bond is searched with chirality off and only its candidate hits are re-parsed and checked with
   chirality; tetrahedral-only queries use the native path. The cost is one full parse per candidate, so
   a broad E/Z query on a large library pays for every candidate.
6. SMARTS is literal: a Kekulé SMARTS does not match aromatic targets. Molblock queries drop explicit
   hydrogens, so a sketcher's hydrogens do not become required atoms.
7. All families are bit vectors. Count-based fingerprints are not supported.

Memory: 258 bytes per molecule per 2048-bit family on top of the substructure library. Saving a snapshot
costs more than holding the index: at 500k molecules with three families the process peaked near 4 GB
resident while writing the 694 MB snapshot, against about 0.9 GB during the rebuild itself. A snapshot of an
older format is ignored and the index is rebuilt from the database; the next save writes the current
format.

A worked mapping of a compound registry onto these primitives is in `docs/examples/registry-adapter.md`.

## Absence check

`absence-check` reads every SMILES and exact key in the sidecar and searches for each one, as raw bytes and as the hex digits PostgreSQL prints for `bytea` (`\x...` in `pg_dump -Fp` COPY data and in statement logs), in three places: the tenant's database columns, a plain dump (`--dump PATH` or `--run-pg-dump`), and a statement log (`--log PATH`). Any hit fails the run.

A negative control proves each leg can see a leak. The command plants a control row whose ciphertext is a fixed plaintext marker for the tenant (seq 0, tombstoned, so no vault reads it) and leaves it in place. The database leg and every dump leg must find it, or the run fails. `--run-pg-dump` plants first and then dumps. For `--dump PATH`, run the check once without `--dump` to plant the control, then take the dump with `pg_dump -Fp --compress=0`, then run the check with `--dump`. If `--run-pg-dump` cannot run `pg_dump`, the run fails. The log leg carries no control, because the log is captured before the check runs. The JSON reports its hits and `"log_control": "not applicable"`.

Tests: `uv run pytest`. Set `MOLSEAL_TEST_DATABASE_URL` to also run the PostgreSQL tests.

## Results

500,000 ChEMBL 37 rows against the RDKit PostgreSQL cartridge (`Release_2025_03_3`), Docker VM with 6 CPUs
and 15.6 GiB, arm64. All four parity gates passed (fingerprint bytes; substructure counts, with
`count_substructure`, hit lists and the cartridge `count(*)` all equal; id sets; similarity ids and scores),
and every one of the 72 substructure queries hits at least one row. Files, metadata, memory peaks and the
full-ChEMBL estimate: [`docs/results/2026-10-13-500k/`](docs/results/README.md).
2026-10-14: parity-only run for the search surface, gates 5–10, in `docs/results/2026-10-14-500k-search-surface/`.
Times are seconds; each query's time is the median of 5 repeats, and p50 (median) and p95 (nearest rank)
are taken across queries.

| side | where | mode | threads | op | cap | queries | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| cartridge | vm | cold | 1 | consumer_path | uncapped | 5 | 39.2377 | 39.5751 |
| cartridge | vm | cold | 1 | count | uncapped | 72 | 0.1043 | 6.1842 |
| cartridge | vm | cold | 1 | similar | uncapped | 50 | 0.0091 | 0.0145 |
| cartridge | vm | cold | 1 | substructure | capped | 72 | 0.0306 | 0.2874 |
| cartridge | vm | cold | 1 | substructure | uncapped | 72 | 0.1097 | 6.2286 |
| cartridge | vm | cold | 1 | topk100 | capped | 50 | 0.0130 | 0.0204 |
| cartridge | vm | warm | 1 | consumer_path | uncapped | 5 | 39.0110 | 39.1015 |
| cartridge | vm | warm | 1 | count | uncapped | 72 | 0.1023 | 6.0929 |
| cartridge | vm | warm | 1 | similar | uncapped | 50 | 0.0094 | 0.0151 |
| cartridge | vm | warm | 1 | substructure | capped | 72 | 0.0301 | 0.2835 |
| cartridge | vm | warm | 1 | substructure | uncapped | 72 | 0.1064 | 6.1174 |
| cartridge | vm | warm | 1 | topk100 | capped | 50 | 0.0130 | 0.0207 |
| molseal | host | warm | -1 | count | uncapped | 72 | 0.0095 | 0.5374 |
| molseal | host | warm | -1 | end_to_end | capped | 72 | 0.0237 | 0.6327 |
| molseal | host | warm | -1 | similar | uncapped | 50 | 0.0123 | 0.0139 |
| molseal | host | warm | -1 | substructure | capped | 72 | 0.0054 | 0.0348 |
| molseal | host | warm | -1 | substructure | uncapped | 72 | 0.0125 | 0.6080 |
| molseal | host | warm | -1 | topk100 | capped | 50 | 0.0160 | 0.0184 |
| molseal | host | warm | 1 | count | uncapped | 72 | 0.0582 | 4.7110 |
| molseal | host | warm | 1 | similar | uncapped | 50 | 0.0120 | 0.0122 |
| molseal | host | warm | 1 | substructure | capped | 72 | 0.0124 | 0.1543 |
| molseal | host | warm | 1 | substructure | uncapped | 72 | 0.0610 | 4.7600 |
| molseal | host | warm | 1 | topk100 | capped | 50 | 0.0151 | 0.0162 |
| molseal | vm | cold | -1 | count | uncapped | 72 | 0.0174 | 1.3943 |
| molseal | vm | cold | -1 | end_to_end | capped | 72 | 0.0279 | 1.5601 |
| molseal | vm | cold | -1 | similar | uncapped | 50 | 0.0221 | 0.0223 |
| molseal | vm | cold | -1 | substructure | capped | 72 | 0.0060 | 0.0552 |
| molseal | vm | cold | -1 | substructure | uncapped | 72 | 0.0205 | 1.9122 |
| molseal | vm | cold | -1 | topk100 | capped | 50 | 0.0254 | 0.0265 |
| molseal | vm | cold | 1 | count | uncapped | 72 | 0.0691 | 5.8718 |
| molseal | vm | cold | 1 | similar | uncapped | 50 | 0.0226 | 0.0233 |
| molseal | vm | cold | 1 | substructure | capped | 72 | 0.0157 | 0.2103 |
| molseal | vm | cold | 1 | substructure | uncapped | 72 | 0.0719 | 6.0450 |
| molseal | vm | cold | 1 | topk100 | capped | 50 | 0.0260 | 0.0272 |
| molseal | vm | warm | -1 | count | uncapped | 72 | 0.0167 | 1.3607 |
| molseal | vm | warm | -1 | end_to_end | capped | 72 | 0.0275 | 1.5352 |
| molseal | vm | warm | -1 | similar | uncapped | 50 | 0.0221 | 0.0225 |
| molseal | vm | warm | -1 | substructure | capped | 72 | 0.0060 | 0.0548 |
| molseal | vm | warm | -1 | substructure | uncapped | 72 | 0.0209 | 1.5232 |
| molseal | vm | warm | -1 | topk100 | capped | 50 | 0.0255 | 0.0266 |
| molseal | vm | warm | 1 | count | uncapped | 72 | 0.0708 | 5.7643 |
| molseal | vm | warm | 1 | similar | uncapped | 50 | 0.0223 | 0.0234 |
| molseal | vm | warm | 1 | substructure | capped | 72 | 0.0170 | 0.2102 |
| molseal | vm | warm | 1 | substructure | uncapped | 72 | 0.0745 | 5.9177 |
| molseal | vm | warm | 1 | topk100 | capped | 50 | 0.0256 | 0.0272 |

- **side**: `molseal` is encrypted rows with search in process; `cartridge` is the RDKit PostgreSQL cartridge.
- **where**: `vm` is the head-to-head inside the Docker VM under one CPU and memory budget. molseal runs in a
  container in the VM; the cartridge database runs in the VM, but its Python client runs on the Mac through
  the Docker port forward (the round trip is in its times). `host` is molseal native on the Mac, reported separately.
- **mode**: see "What cold means" below.
- **threads**: molseal with 1 thread or all cores (`-1`); the cartridge is one PostgreSQL backend on one
  core (`max_parallel_workers_per_gather=0`).
- **op**: `substructure` returns ids, `count` returns a count, `similar` is Tanimoto >= 0.7 on Morgan r2/2048
  (the cartridge returns ids only, molseal ids with scores), `topk100` is the 100 nearest (no parity gate),
  `end_to_end` adds molseal's tenant-state check and a fetch of up to 1000 hit rows, `consumer_path` is the
  cartridge's unindexed `mol_from_smiles(smiles) @> q` (5 queries, one run each), not the baseline.
- **cap**: `capped` stops at 1000 hits (`maxResults=1000` vs `LIMIT 1000`); `uncapped` returns every hit.
- **queries**: how many queries the row's percentiles cover.

What cold means here. Cartridge `cold` is right after a container restart, with `pg_buffercache` showing
shared buffers nearly empty (196 pages); the Docker VM's page cache is not dropped, and each query runs
once untimed as `count(*)` and once as a warm-up before the timed repeats, so cold and warm cartridge
rows are nearly equal. Cartridge `warm` adds `pg_prewarm` of the table and both indexes. molseal `cold`
and `warm` are the same procedure (a fresh process loads the snapshot, then one untimed warm-up per
query); molseal's real cold cost is loading the 169 MB snapshot: `cold_load_s` 0.73 s and 0.80 s in the
VM, 0.82 s host-native.

Leak report (500k rows): ciphertext size class correlates with heavy-atom count at Pearson r = 0.7767, but
predicting the 10-heavy-atom bucket from the size class is right 43.0% of the time against a 42.7% majority
baseline. Size classes: 256 B 490,357 rows, 512 B 8,278, 1024 B 1,347, 2048 B 15.

Memory peaks (`docker stats`): cartridge container 1.25 GiB during the GiST builds (load run, ~30 s samples)
and 1.30 GiB during timing; molseal bench container 0.62 GiB (process `max_rss` 1118 MB); molseal
host-native process `max_rss` 1402 MB.

## What is deliberately not built

- Key rotation
- Molfile storage
- Multi-process invalidation
- Async API
- KMS key providers
- Any consumer adapter
- Count-based fingerprints
- A similarity index (every query scans every fingerprint)
- Readers-writer lock

## License

BSD-3-Clause.

## Data attribution

Benchmark queries and results derive from ChEMBL 37, EMBL-EBI, CC BY-SA 3.0, https://www.ebi.ac.uk/chembl/ .
Slices are generated locally and not redistributed. The published query sets
(`docs/results/**/queries_*.json`) hold ChEMBL SMILES and fragments of them and are CC BY-SA 3.0, not BSD-3.
