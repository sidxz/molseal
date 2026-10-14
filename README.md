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
and 15.6 GiB, arm64. All four parity gates passed (fingerprint bytes, substructure counts, id sets,
similarity ids and scores). Files, metadata and memory peaks: [`docs/results/2026-10-13-500k/`](docs/results/README.md).
Times are seconds; p50 and p95 are taken across queries of each query's median.

| side | where | mode | threads | op | cap | queries | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|
| cartridge | vm | cold | 1 | consumer_path | uncapped | 5 | 13.7233 | 13.8057 |
| cartridge | vm | cold | 1 | count | uncapped | 72 | 0.3149 | 6.2756 |
| cartridge | vm | cold | 1 | similar | uncapped | 50 | 0.0089 | 0.0147 |
| cartridge | vm | cold | 1 | substructure | capped | 72 | 0.0454 | 0.7133 |
| cartridge | vm | cold | 1 | substructure | uncapped | 72 | 0.3199 | 6.4142 |
| cartridge | vm | cold | 1 | topk100 | capped | 50 | 0.0130 | 0.0208 |
| cartridge | vm | warm | 1 | consumer_path | uncapped | 5 | 13.7106 | 13.9188 |
| cartridge | vm | warm | 1 | count | uncapped | 72 | 0.3149 | 6.2829 |
| cartridge | vm | warm | 1 | similar | uncapped | 50 | 0.0091 | 0.0154 |
| cartridge | vm | warm | 1 | substructure | capped | 72 | 0.0448 | 0.7115 |
| cartridge | vm | warm | 1 | substructure | uncapped | 72 | 0.3196 | 6.4232 |
| cartridge | vm | warm | 1 | topk100 | capped | 50 | 0.0132 | 0.0209 |
| molseal | host | warm | -1 | count | uncapped | 72 | 0.0259 | 0.4920 |
| molseal | host | warm | -1 | end_to_end | capped | 72 | 0.0334 | 0.6503 |
| molseal | host | warm | -1 | similar | uncapped | 50 | 0.0121 | 0.0123 |
| molseal | host | warm | -1 | substructure | capped | 72 | 0.0077 | 0.0438 |
| molseal | host | warm | -1 | substructure | uncapped | 72 | 0.0314 | 0.6044 |
| molseal | host | warm | -1 | topk100 | capped | 50 | 0.0154 | 0.0165 |
| molseal | host | warm | 1 | count | uncapped | 72 | 0.1910 | 4.8187 |
| molseal | host | warm | 1 | similar | uncapped | 50 | 0.0119 | 0.0122 |
| molseal | host | warm | 1 | substructure | capped | 72 | 0.0241 | 0.3588 |
| molseal | host | warm | 1 | substructure | uncapped | 72 | 0.2047 | 4.8274 |
| molseal | host | warm | 1 | topk100 | capped | 50 | 0.0149 | 0.0161 |
| molseal | vm | cold | -1 | count | uncapped | 72 | 0.0570 | 1.3771 |
| molseal | vm | cold | -1 | end_to_end | capped | 72 | 0.0793 | 1.5321 |
| molseal | vm | cold | -1 | similar | uncapped | 50 | 0.0223 | 0.0226 |
| molseal | vm | cold | -1 | substructure | capped | 72 | 0.0088 | 0.1122 |
| molseal | vm | cold | -1 | substructure | uncapped | 72 | 0.0680 | 1.5268 |
| molseal | vm | cold | -1 | topk100 | capped | 50 | 0.0257 | 0.0268 |
| molseal | vm | cold | 1 | count | uncapped | 72 | 0.2313 | 5.7343 |
| molseal | vm | cold | 1 | similar | uncapped | 50 | 0.0216 | 0.0224 |
| molseal | vm | cold | 1 | substructure | capped | 72 | 0.0302 | 0.4720 |
| molseal | vm | cold | 1 | substructure | uncapped | 72 | 0.2440 | 5.8672 |
| molseal | vm | cold | 1 | topk100 | capped | 50 | 0.0247 | 0.0260 |
| molseal | vm | warm | -1 | count | uncapped | 72 | 0.0565 | 1.3812 |
| molseal | vm | warm | -1 | end_to_end | capped | 72 | 0.0787 | 1.5550 |
| molseal | vm | warm | -1 | similar | uncapped | 50 | 0.0236 | 0.0268 |
| molseal | vm | warm | -1 | substructure | capped | 72 | 0.0089 | 0.1102 |
| molseal | vm | warm | -1 | substructure | uncapped | 72 | 0.0709 | 1.5526 |
| molseal | vm | warm | -1 | topk100 | capped | 50 | 0.0266 | 0.0306 |
| molseal | vm | warm | 1 | count | uncapped | 72 | 0.2377 | 5.7004 |
| molseal | vm | warm | 1 | similar | uncapped | 50 | 0.0253 | 0.0255 |
| molseal | vm | warm | 1 | substructure | capped | 72 | 0.0312 | 0.4854 |
| molseal | vm | warm | 1 | substructure | uncapped | 72 | 0.2502 | 5.8159 |
| molseal | vm | warm | 1 | topk100 | capped | 50 | 0.0255 | 0.0295 |

- **side**: `molseal` is encrypted rows with search in process; `cartridge` is the RDKit PostgreSQL cartridge.
- **where**: `vm` runs inside the Docker VM under the same CPU and memory budget (the head-to-head); `host` is molseal native on the Mac, reported separately.
- **mode**: `cold` is right after a database container restart (cartridge) or a fresh process loading the snapshot (molseal); `warm` is after `pg_prewarm` (cartridge) or a first pass (molseal).
- **threads**: molseal with 1 thread or all cores (`-1`); the cartridge is one PostgreSQL backend.
- **op**: `substructure` returns ids, `count` returns a count, `similar` is Tanimoto >= 0.7 on Morgan r2/2048, `topk100` is the 100 nearest, `end_to_end` adds molseal's tenant-state check and a fetch of up to 1000 hit rows, `consumer_path` is the cartridge's unindexed `mol_from_smiles(smiles) @> q` (5 queries, one run each), not the baseline.
- **cap**: `capped` stops at 1000 hits (`maxResults=1000` vs `LIMIT 1000`); `uncapped` returns every hit.
- **queries**: how many queries the row's percentiles cover.

Leak report (500k rows): ciphertext size class correlates with heavy-atom count at Pearson r = 0.7767, but
predicting the 10-heavy-atom bucket from the size class is right 43.0% of the time against a 42.7% majority
baseline. Size classes: 256 B 490,357 rows, 512 B 8,278, 1024 B 1,347, 2048 B 15.

Memory peaks (`docker stats` every ~30 s): cartridge container 1.25 GiB during the GiST builds and
1.44 GiB during timing; molseal bench container 0.78 GiB (process `max_rss` 1117 MB); molseal
host-native process `max_rss` 1470 MB.

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
Slices are generated locally and not redistributed.
