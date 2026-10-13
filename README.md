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

Reserved for ChEMBL (CC BY-SA 3.0). Filled in when the benchmark data is added.
