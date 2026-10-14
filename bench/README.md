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

    MOLSEAL_LOG_STATEMENT=all docker compose up -d --force-recreate db   # repo-root compose, :5439
    uv run python -m bench.gate      # load 10k, absence check in DB, pg_dump and statement log, leak report
    docker compose stop db

Must print `GATE OK` before any benchmark is trusted. The fresh container runs with
`log_statement=all`, so `docker compose logs db` holds exactly this run's statements; the gate saves
them to `bench/results/gate_10k_pg.log` before the control row is planted, scans them, and records
`log_statements` (must be > 0, or the leg proved nothing) and `log_hits` (must be 0) in `gate_10k.json`.

## 4. One database profile at a time

    docker compose -f bench/compose.yml --profile cartridge up -d    # cartridge-db, :5440
    docker compose -f bench/compose.yml --profile molseal up -d      # molseal-db, :5441

Bring up only the profile you are loading or timing. Stop it (`down`) before switching, except for
the parity run (step 6), which needs both.

## 5. Load each side (host-native is fine; load times are not compared)

Each load starts from an empty database: `bench/run_m2.sh` step 3 and step 4 drop their own volume
first, and step 3 also deletes `<slice>.progress.json`, `<slice>.ids.tsv` and `bench/data/snap`.
`load_cartridge` refuses a non-empty `mols` table or existing GiST indexes and checks that every
slice row was inserted. The resumable molseal loader records `{rows_done, chunk}` and refuses to
resume with a different `--chunk`. One tenant per N (`bench/results/tenant_<N>.txt`).

    uv run python -m bench.load_cartridge --slice bench/data/slice_500000.tsv
    export MOLSEAL_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5441/molseal
    export MOLSEAL_KEY_FILE=bench/results/bench.key MOLSEAL_SNAPSHOT_DIR=bench/data/snap
    export MOLSEAL_TENANT=$(python3 -c 'import uuid; print(uuid.uuid4())')
    uv run molseal keygen "$MOLSEAL_KEY_FILE"      # once
    uv run python -m bench.load_molseal --slice bench/data/slice_500000.tsv

Keep the same `MOLSEAL_TENANT` for every later step (the driver keeps it in `tenant_<N>.txt`).
Loads are resumable.

## 6. Build the query set, then parity (both profiles up)

    uv run python -m bench.queries --slice bench/data/slice_500000.tsv
    uv run python -m bench.parity --n 500000

Parity checks fingerprint bytes, substructure counts (`count_substructure`, the hit-list length and
the cartridge `count(*)` must all agree) and id sets (queries over 10k hits are recorded as skipped),
and similarity ids and scores against the cartridge. `topk100` has no parity gate. Every BRICS
fragment query is checked to match the molecule it was cut from, so a zero-hit query is a bug. **A run with failed parity publishes nothing:** no timing is reported or
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

## 8. The driver

`bench/run_m2.sh` runs steps 1 to 10 in order and stops at the first failure.

    bash bench/run_m2.sh 2>&1 | tee bench/results/run_m2_500000.log     # everything
    STEP=6 bash bench/run_m2.sh                                          # resume from step 6
    STEPS="2 5 6 7 8 9 10" bash bench/run_m2.sh                          # these steps only

Only steps 3 and 4 reset a volume, so `STEPS` without 3 and 4 reuses the existing loads. Step 7
deletes `timings_<N>_*.json` first, so the report covers only the run that wrote them. `run_bench`
refuses a parity file older than the newest `load_*_<N>.json`. A memory sampler appends
`docker stats` to `mem_<N>.log` every 5 s.

## 9. Report

    uv run python -m bench.run_bench --n 500000 --report

Emits the Markdown table from the per-run JSON files in `bench/results`.

## 10. Labels

- **side**: `molseal` (encrypted rows, search in process) or `cartridge` (RDKit PostgreSQL cartridge).
- **where**: `vm` means the work happens in the Docker VM under one CPU and memory budget (the
  head-to-head). For molseal the search runs in the `bench` container inside the VM. For the
  cartridge the search runs in the database container inside the VM, but the Python client that
  sends the queries runs on the macOS host through the Docker port forward. `host` is molseal
  native on the host, reported separately.
- **mode**: see "What cold means" below.
- **threads**: molseal `num_threads=1` and `num_threads=-1` (all cores) are reported separately.
  The cartridge runs with `max_parallel_workers_per_gather=0`, so every cartridge query is one
  backend on one core (threads=1); the run records the setting in its metadata.
- **cap**: capped runs compare `GetMatches(maxResults=1000)` with `... LIMIT 1000`, on count only.
- **op**: substructure and similarity. Each query has one warm-up then at least 5 repeats; per-query
  median, then p50 and p95 across queries. Cartridge substructure is the indexed `m @> q` (the fair
  baseline); the unindexed `mol_from_smiles(smiles) @> q` is labeled "consumer's current path", not
  "baseline". Scope is "search-only" for both sides, plus "end-to-end" for molseal (cursor check and
  a `WHERE id = ANY(:ids)` fetch of the hit rows).

Load and build times, resident memory (`ru_maxrss`), snapshot size and cold snapshot load are also
recorded; load times are labeled non-comparable (parallel native vs single-threaded in the VM).
Inside the container neither `git` nor `docker` is available, so git SHA, image architecture and
VM size show as "unavailable" there; they are read on the host by hand after the run and written
into `docs/results/README.md`.

`similar` differs in what it returns: the cartridge returns ids only, molseal computes and returns
ids with Tanimoto scores.

## What cold means

- **Cartridge `cold`**: the database container was just restarted and `pg_buffercache` shows
  shared buffers nearly empty (the run refuses above 1% of them). The Docker VM's page cache is
  **not** dropped, so table and index pages can still come from OS memory. Before the timed
  repeats each query runs once untimed as `count(*)` (for the hit count) and once more as the
  warm-up, so every timed sample is at least the third execution.
- **Cartridge `warm`**: `pg_prewarm` of the table and both GiST indexes, then the same procedure.
- **molseal `cold` and `warm` are the same procedure**: a fresh process loads the snapshot, then
  each query gets one untimed warm-up before the timed repeats. The two labels are kept to pair
  with the cartridge rows, not because molseal does anything different. molseal's real cold cost
  is `cold_load_s` in the timing JSON: the time to load the snapshot into a fresh process.

## Data attribution

ChEMBL 37, EMBL-EBI, CC BY-SA 3.0, https://www.ebi.ac.uk/chembl/ . Slices are generated locally and
not redistributed. RDKit is pinned to 2025.03.3 (PyPI spelling `2025.3.3`), matching the cartridge
`Release_2025_03_3`.
