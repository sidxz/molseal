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
- Tautomer-insensitive search
- Stereo-aware search
- Molfile storage
- Multi-process invalidation
- Async API
- KMS key providers
- Any consumer adapter
- Readers-writer lock

## License

BSD-3-Clause.

## Data attribution

Benchmark queries and results derive from ChEMBL 37, EMBL-EBI, CC BY-SA 3.0, https://www.ebi.ac.uk/chembl/ .
Slices are generated locally and not redistributed. The published query sets
(`docs/results/**/queries_*.json`) hold ChEMBL SMILES and fragments of them and are CC BY-SA 3.0, not BSD-3.
