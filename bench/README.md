# molseal benchmark: procedure

Head-to-head of molseal (encrypted rows, in-process RDKit search) against the RDKit PostgreSQL
cartridge on ChEMBL slices. All commands run from the repo root. Both sides must share one CPU and
memory budget, so the timed comparison runs inside the Docker VM.

## 1. VM sizing

Docker Desktop, Settings, Resources: Memory **16 GB for 500k rows, 24 GB for full ChEMBL (~2.4M)**;
CPUs: all. Apply and restart Docker. Only one database profile runs during timing.

## 2. Fetch slices

    uv run python -m bench.fetch_chembl --n 10000
    uv run python -m bench.fetch_chembl --n 500000

Writes `bench/data/slice_<N>.tsv` (git-ignored, never redistributed).

## 3. The 10k gate

    docker compose up -d db          # repo-root compose file, port 5439
    uv run python -m bench.gate      # load 10k, absence check incl. pg_dump, leak report
    docker compose down

Must print `GATE OK` before any benchmark is trusted.

## 4. One database profile at a time

    docker compose -f bench/compose.yml --profile cartridge up -d    # cartridge-db, :5440
    docker compose -f bench/compose.yml --profile molseal up -d      # molseal-db, :5441

Bring up only the profile you are loading or timing. Stop it (`down`) before switching, except for
the parity run (step 6), which needs both.

## 5. Load each side (host-native is fine; load times are not compared)

    uv run python -m bench.load_cartridge --slice bench/data/slice_500000.tsv
    export MOLSEAL_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5441/molseal
    export MOLSEAL_KEY_FILE=bench/results/bench.key MOLSEAL_SNAPSHOT_DIR=bench/data/snap
    export MOLSEAL_TENANT=$(python3 -c 'import uuid; print(uuid.uuid4())')
    uv run molseal keygen "$MOLSEAL_KEY_FILE"      # once
    uv run python -m bench.load_molseal --slice bench/data/slice_500000.tsv

Keep the same `MOLSEAL_TENANT` for every later step. Loads are resumable.

## 6. Build the query set, then parity (both profiles up)

    uv run python -m bench.queries --slice bench/data/slice_500000.tsv
    uv run python -m bench.parity --n 500000

Parity checks fingerprint bytes, substructure counts and id sets, and similarity ids and scores
against the cartridge. **A run with failed parity publishes nothing:** no timing is reported or
copied into any write-up until parity passes.

## 7. Timing order (one profile up at a time)

1. Cartridge cold: `docker compose -f bench/compose.yml restart cartridge-db`, then
   `uv run python -m bench.run_bench --n 500000 --side cartridge --mode cold`
2. Cartridge warm: same with `--mode warm --prewarm`
3. Stop cartridge, start molseal-db. molseal in the VM, cold then warm:

       docker compose -f bench/compose.yml --profile molseal --profile bench run --rm bench \
         -m bench.run_bench --n 500000 --side molseal --where vm --mode cold
       docker compose -f bench/compose.yml --profile molseal --profile bench run --rm bench \
         -m bench.run_bench --n 500000 --side molseal --where vm --mode warm

   (Pass the tenant into the container with `-e MOLSEAL_TENANT=$MOLSEAL_TENANT` after `run --rm`.)
4. molseal host-native (labeled separately): same commands with `--where host`.

Add `--threads 1 -1` to change thread settings (default is both) and `--repeats` (default 5).

## 8. Report

    uv run python -m bench.run_bench --n 500000 --report

Emits the Markdown table from the per-run JSON files in `bench/results`.

## 9. Labels

- **side**: `molseal` (encrypted rows, search in process) or `cartridge` (RDKit PostgreSQL cartridge).
- **where**: `vm` is inside a container on the Docker VM, sharing CPU and memory with the cartridge
  database (the head-to-head); `host` is native on the host, reported separately.
- **mode**: `cold` is after a container restart; `warm` is after `pg_prewarm` of table and indexes
  (cartridge) or after the first pass (molseal).
- **threads**: molseal `num_threads=1` and `num_threads=-1` (all cores) are reported separately,
  since a PostgreSQL backend is single-threaded.
- **cap**: capped runs compare `GetMatches(maxResults=1000)` with `... LIMIT 1000`, on count only.
- **op**: substructure and similarity. Each query has one warm-up then at least 5 repeats; per-query
  median, then p50 and p95 across queries. Cartridge substructure is the indexed `m @> q` (the fair
  baseline); the unindexed `mol_from_smiles(smiles) @> q` is labeled "consumer's current path", not
  "baseline". Scope is "search-only" for both sides, plus "end-to-end" for molseal (cursor check and
  a `WHERE id = ANY(:ids)` fetch of the hit rows).

Load and build times, resident memory (`ru_maxrss`), snapshot size and cold snapshot load are also
recorded; load times are labeled non-comparable (parallel native vs single-threaded in the VM).
Inside the container `docker` is unavailable, so image architecture and VM size show as
"unavailable" there; the host-side driver fills them in.

## Data attribution

ChEMBL 37, EMBL-EBI, CC BY-SA 3.0, https://www.ebi.ac.uk/chembl/ . Slices are generated locally and
not redistributed. RDKit is pinned to 2025.03.3 (PyPI spelling `2025.3.3`), matching the cartridge
`Release_2025_03_3`.
