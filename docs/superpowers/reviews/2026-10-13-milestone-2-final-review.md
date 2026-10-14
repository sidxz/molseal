# Milestone 2 (m2-bench) final review

Range 607900f..564cd7d (11 commits, HEAD 564cd7d, tag milestone-2). Read in three passes: library diff, bench code with tests, published docs and data (parity JSON, timing JSONs, mem log, queries) checked against each other. Read-only; nothing in the checkout was changed.

Test run (dev Postgres on :5439 up):
- `uv run pytest -q` -> `123 passed, 5 skipped in 1.66s`. The 5 skips: 4 x `tests/test_store_pg.py` (MOLSEAL_TEST_DATABASE_URL not set) and 1 x `tests/bench/test_load_cartridge.py:23` (MOLSEAL_CARTRIDGE_URL not set).
- With `MOLSEAL_TEST_DATABASE_URL=postgresql+psycopg://molseal:molseal@localhost:5439/molseal`: `127 passed, 1 skipped in 3.23s` (only the cartridge test skips). The claimed "123 passed, 5 skipped" is the run without the PG URL, so the four PostgreSQL tests, including the new batch cursor-lock test, were not part of that claim; they do pass.

## Strengths

- `SqlStore.put_structures` is right where it matters: it takes the tenant row lock first (`UPDATE ... seq_cursor + n RETURNING`), so the per-row compare-and-set read cannot go stale, seqs are consecutive and commit-ordered, the live-count delta covers insert, revive-after-delete and update, keys are replaced, and any conflict rolls the whole batch back. The PG test pins the "rolled-back batch consumes no numbers" case.
- `Vault._put_chunk` validates duplicates and canonicalizes before any write, re-reads versions and re-seals with fresh nonces on each retry, and touches the index only after the store commit returns. `_advance(first, last)` keeps the single-row callers working.
- The CLI and loader write sidecar lines only after the chunk commits; deterministic uuid5 ids make a re-sent chunk an update instead of a duplicate, and that case is tested.
- The parity gates do detect real errors: a missing cartridge row counts as a fingerprint mismatch (`None != bytes`), the counts, id sets and scores are compared both ways, and `ok` is the AND of every gate. `run_bench` refuses to time without `parity_<N>.json` `ok: true`.
- Every published timing row carries side/where/mode/threads/op/capped (checked: 0 unlabeled rows across the 5 JSON files), and `summarize()` over the published JSON reproduces `timings_500000.md` byte for byte. The README table equals that file.
- Memory claims match `mem_500000.log` (recomputed: cartridge 409.7 MiB insert, 1283 MiB GiST window, 1476 MiB timing peak; molseal-db 504 MiB in parity; bench container 800 MiB). Leak numbers match `leak_500000.json`.
- The generic rule holds: nothing under `src/molseal` names a consumer, tenants stay opaque UUIDs, exact keys stay caller-defined, and `src/` does not import `bench`. The only InChIKey computation is still the M1-accepted CLI exception.
- Hygiene: `git ls-files` has no `*.key`, nothing under `bench/data`, `bench/results` or `talks/`. The Dockerfile copies only `pyproject.toml uv.lock README.md src bench`, and `.dockerignore` keeps `bench/data` and `bench/results` out. Trailers are correct and there are no session URLs.

## Issues

### Critical

**C1. The cold-label limitation is not stated anywhere, and the molseal cold/warm labels describe a procedure that never ran.**
`README.md:132`, `bench/README.md:83`, `docs/results/README.md` (no mention at all).
- What is wrong: the ruling requires the results README to say that "cold" means only "container restarted and shared_buffers near empty", that the VM page cache is not dropped, and that each query gets an untimed warm-up. No published file says this. `README.md:132` says only "`cold` is right after a database container restart (cartridge) or a fresh process loading the snapshot (molseal); `warm` is after `pg_prewarm` (cartridge) or a first pass (molseal)." On the cartridge side, `run_cartridge` also runs an untimed `count(*)` for each query before the warm-up (`bench/run_bench.py:111`), so every timed sample is at least the third execution. On the molseal side, cold and warm are the same procedure: `run_m2.sh:71-74` starts a fresh `run --rm` container for both modes, `run_bench` has no mode-specific code for molseal, both JSONs record the same `cold_load_s` 0.69, and no "first pass" happens before "warm".
- Why it matters: the published table shows "cold" rows next to "warm" rows that are actually warm. That is the dishonest framing spec 12 forbids, and it breaks an explicit merge condition.
- Fix (docs only): add a "What cold means here" paragraph to `docs/results/README.md` and next to the root README table, covering the restart, the near-empty `pg_buffercache` check (259 pages), the VM page cache that is not dropped, and the untimed count plus warm-up that come before the timed repeats. State that molseal cold and warm are the same procedure and that the real molseal cold cost is `cold_load_s` (0.69 s VM, 0.64 s host) for loading the 169 MB snapshot; quote that number. Change the bench/README and root README label lines to match. Alternatively, drop the molseal "cold" rows from the table.

### Important

**I1. The cartridge "consumer_path" rows are labeled threads=1 but ran on about 3 cores.**
`bench/compose.yml:13-15`, `bench/run_bench.py:102,118-124`, `README.md:131` ("the cartridge is one PostgreSQL backend").
- What is wrong: `max_parallel_workers_per_gather` is left at the PostgreSQL default of 2. `mem_500000.log` shows `bench-cartridge-db-1` at 297-299% CPU exactly at 16:29:15-16:30:19 and 17:00:43-17:01:15. Those are the two 5 x ~13.7 s consumer_path windows. Every other timing sample is at about 100%. The unindexed seq scan used a leader plus 2 parallel workers.
- Why it matters: spec 12 says no multi-core number may appear next to a single-backend number without its label. Here the label itself is wrong, and the published 13.7 s consumer path is about 3x faster than a single backend would be. The indexed rows look single-process from the CPU trace, but nothing enforces or records that.
- Fix: either set `-c max_parallel_workers_per_gather=0` in the compose command (and in the spec's settings list), or record the setting plus the `EXPLAIN` worker count per op and label consumer_path threads=3. Then rerun or relabel the consumer_path rows.

**I2. `count_substructure` is timed and published but never parity-checked.**
`bench/parity.py:79-81`, `bench/run_bench.py:82-83`.
- What is wrong: spec 8.4 gate 2 says "`count_substructure` (chirality off) equals `SELECT count(*)`". The plan (line 1239) and the code instead compare `len(set(vault.substructure(...)) - rejected)`. `CountMatches` is a separate RDKit path (`engine.py:178-185`). The `hits` field of the `count` timing rows is copied from the substructure result, not from the count, so a wrong count would never show up anywhere.
- Why it matters: the `count` rows (p50 0.0565 / 0.2377 s) are published as parity-backed. The `set()` also hides a duplicate-returning `substructure`.
- Fix: in gate 2, also record `vault.count_substructure(q, kind) - len(rejected ∩ hits)` and require it to equal the cartridge count and `len(list(hits))` (list length, not set). In `run_molseal`, take the count row's `hits` from the count call.

**I3. Eight of the 50 BRICS fragment queries are dead: radical artifacts that match nothing.**
`bench/queries.py:40-50`.
- What is wrong: `DeleteSubstructs(fm, [#0])` removes the dummy atoms and leaves their attachment atoms as radicals (`CC[CH]C=O`, `[CH]1C[CH]CC1`, `OCC#C[C]1CN2...`). A SMILES query with radical electrons matches only radicals. Verified: exactly the 8 radical-bearing fragments (frag-001/002/007/013/023/030/036/047) are the 8 with 0 hits on both sides, although each one was cut from a molecule in the slice.
- Why it matters: 16% of the fragment queries test nothing in gates 2 and 3. They pass vacuously, which is Review Focus 4's concern. They also pull the p50 of both sides toward screen-only timings, and spec 8.3's "varied selectivity" is not met.
- Fix: before sanitizing, set each former attachment atom's radical count to 0 (`SetNumRadicalElectrons(0); SetNoImplicit(False)`), or replace the dummies with H. Assert that each fragment matches its parent molecule, and record per-query hits so zero-hit queries are deliberate. Regenerate the queries and rerun parity and timing at 500k (or at least before M3), and say so in the results README.

**I4. Stale state is reused silently across runs and across N, which will publish wrong load and build numbers in M3 and M4 with parity still green.**
`bench/load_cartridge.py:22-25,62-64`, `bench/load_molseal.py:41-48`, `bench/run_m2.sh:17-19,44-53`, `bench/run_bench.py:169,174-176`.
- What is wrong: the Task 9 stale-volume problem is real, and worse at the next N. The 1M slice is a superset of the 500k slice, so with the volume left in place:
  - The cartridge `ON CONFLICT DO NOTHING` turns 500k inserts into no-ops.
  - `CREATE INDEX IF NOT EXISTS` reports about 0.0 s GiST builds, and the indexes grow incrementally.
  - `load_cartridge` reports `rows: n` as attempted, not inserted, and never checks `count(*)`.
  - On the molseal side, `tenant.txt` and `bench.key` are shared across N and the molseal-db volume is kept, so the 1M load turns 500k rows into per-row UPDATEs (the slow path in `put_structures`), and `rows_per_second` is no longer a fresh-load number.
  - Parity passes in both cases because the final row sets are equal, so the wrong numbers would be published.
- Further stale artifacts:
  - The progress file is keyed only by slice path. After a molseal volume reset, `STEP=3` loads 0 rows and exits 0. A reset volume plus an old snapshot in `bench/data/snap` with freshly minted tenant keys makes `Vault.__init__` crash; M1 deferred minor 3 becomes load-bearing here.
  - `run_bench` accepts any old `parity_<N>.json` with `ok: true`, even after a reload.
  - `--report` globs every `timings_<N>_*.json`, so leftovers from an aborted or older run go into the published table.
- Fix:
  - In step 3 and step 4, `down -v` the profile (or refuse a non-empty `mols` table or tenant) and delete `<slice>.progress.json` and `.ids.tsv`.
  - Use one tenant per N (`tenant_<N>.txt`) and clear `bench/data/snap`.
  - Have `load_cartridge` assert `SELECT count(*) = n - 0` after the insert and fail if the indexes already exist.
  - Have `run_bench` refuse a parity file older than the newest `load_*_<N>.json`.
  - Have `--report` read only the file names the driver wrote in this run, or move stale files aside at step 7.

**I5. The resumable loader can silently skip rows when `--chunk` changes between runs.**
`bench/load_molseal.py:43-48,61`.
- What is wrong: progress is stored as `done_chunks`, and the skip loop multiplies it by the current `chunk`. Resuming at chunk 10 with a larger `--chunk` skips rows that were never loaded and still exits 0. (A smaller chunk only re-sends rows, which is harmless.)
- Why it matters: Review Focus 2 is about exactly this property. Parity would catch it later, but only after the whole load and the GiST builds.
- Fix: store `{"rows_done": ..., "chunk": ...}` and skip by rows, or refuse a mismatched chunk size. Add a test.

**I6. The statement-log leg of the absence check still has never run.**
`bench/gate.py:46-50`, `docker-compose.yml` (no `log_statement=all`), `bench/compose.yml:28` (`log_statement=none`).
- What is wrong: spec 8.2 says `molseal-db` uses `log_statement=all` during the absence check, and spec 9 lists three legs. M1's final review (line 72) asked for the 10k run "with `docker compose exec db pg_dump` and a real `log_statement=all` capture". The plan dropped the log leg with no ruling in either ledger, and `gate.py` never passes `--log`.
- Why it matters: "absence in full statement logs" is one of the three things spec section 1 says the POC proves. `gate_10k.json` is described honestly ("DB and a real pg_dump"), so this is a coverage gap, not a false claim.
- Fix: start `db` with `-c log_statement=all` for step 2, capture its log (`docker compose logs db` or `log_destination`), pass `--log`, and record `log_hits` in `gate_10k.json`. Or record an explicit ruling that defers it.

### Minor

1. `src/molseal/cli.py:85,106`: the skipped count goes back through a function attribute (`_parse_smiles_file.skipped`). It is global and survives across calls, and it is unset if the generator is not drained. Use a small counter object or return the count from a wrapper.
2. `src/molseal/store.py:209`: `put_structures` takes `tenant_id` from `batch[0]` and writes every row under it. A mixed-tenant batch passed straight to the public store protocol would be filed under the wrong tenant. Add `if any(r.tenant_id != tenant_id ...): raise ValueError`.
3. `src/molseal/vault.py:123-130`: duplicate detection and atomicity are per chunk, not per `put_many` call. A duplicate in a later chunk is silently last-wins, and a failure in chunk k leaves chunks 1..k-1 committed while the exception loses `total`. Say so in the docstring, or carry the committed count on the exception.
4. No test covers the `put_many` conflict-retry path (fresh nonces on retry, index updated once). A monkeypatched `put_structures` that raises `VersionConflict` once would pin it.
5. `bench/parity.py:62-95` (`run`) has no unit test. The rejected-row subtraction, source exclusion, the gate-3 limit and the `ok` aggregation are verified only by the one live run. Pull the comparisons into a pure function and test it with fakes. Gate 3 also silently omits queries over 10k hits; record them as `{"skipped": "count > 10000"}` so the skip is visible (Review Focus 4).
6. `bench/run_bench.py:146`: `percentile(xs, 50)` with 72 queries is nearest-rank (index 36, the upper middle) while per-query values use a true median. `round()` is also banker's rounding. Use `statistics.median` for p50, or document the nearest-rank method.
7. `bench/run_m2.sh:64-65` with `README.md:130`: the cartridge "vm" client runs on the macOS host through the Docker port forward, while molseal "vm" runs inside the VM. The impact is small (uncapped minus count is about 0.14 s at p95), but "vm runs inside the Docker VM under the same CPU and memory budget" is untrue for the cartridge client. Either run the cartridge timing through the `bench` container too, or relabel it.
8. Cartridge `similar` returns ids only, while molseal `similar` computes and returns scores. `topk100` has no parity check (the `<%>` KNN is not compared with molseal's top 100). Note both, or add a top-k id-set gate.
9. `bench/README.md:96` says "the host-side driver fills them in", but nothing fills in git SHA, architecture or VM size; they were typed into `docs/results/README.md` by hand. The VM git SHA is inferred. Bake `GIT_SHA` into the bench image as a build arg and drop the unused `git` apt package from the Dockerfile.
10. The 2.4M memory estimate (6-7 GB cartridge, about 5.5 GB molseal) lives only in the ledger and the Task 9 report. Nothing in `docs/` publishes it, so nothing there overclaims, but add it to `docs/results/README.md` labeled as an estimate and give its basis (about 4.8x linear scaling, shared_buffers 4 GB no longer holding the relation).
11. `queries_500000.json` publishes 50 whole ChEMBL molecules as similarity queries, plus fragments. That is a query set, not the dataset, so it is acceptable. Because ChEMBL is CC BY-SA 3.0 (share-alike) and the repo is BSD-3, state in the README attribution that `docs/results/**/queries_*.json` is CC BY-SA 3.0.
12. `bench/load_molseal.py:57-61`: a crash between the sidecar write and the progress write duplicates that chunk's sidecar lines on resume. This is harmless for parity sampling, but `leak-report` counts those rows twice. Dedupe on read or truncate the sidecar to `done * chunk` lines on resume.
13. The memory sampler interval (30 s) cannot resolve the 9-15 s phases. That is documented. Use 5 s, or `docker stats` streaming, for M3.
14. `.dockerignore` could also list `*.key`, `.env`, `talks/` and `snapshots/` as defence in depth. Today's `COPY` lines do not reach them.

M1 deferred minors that this branch makes load-bearing:
- Minor 3, the crash on a bad snapshot, now matters in the stale-state reset scenario in I4.
- Minor 13, `_pack` via `ToBitString`, now matters for M3/M4 ingest throughput (4040 rows/s at 500k is fine; it is worth profiling before 2.4M).
- Minor 9, the store suite on SQLite only, is partly addressed: `put_structures` has one PG test.

The other deferred minors (1, 2, 4-8, 10-12, 14) are not exercised differently by this branch.

## Declined to judge

- The similarity index (bit-count bands) and its brute-force cost: deferred by ruling to `docs/backlog/2026-10-13-generic-search-surface.md`.
- An async `put_many`: the library stays synchronous by ruling and by spec 13.
- Ports 5439/5440/5441, the cartridge UTF-8 initdb, binding `:smi` and `:smic` separately, the root `.dockerignore`: accepted rulings, all applied correctly.
- The CLI computing InChIKey inside `src/molseal/cli.py`: M1-accepted exception, unchanged here except for chunking.
- The non-idempotent `molseal load` CLI (uuid4 ids, so a re-run duplicates rows): pre-existing M1 semantics; the resumable loader is in `bench/`.
- Per-row UPDATE round trips in `put_structures` for existing rows: performance only, reached only on re-sends, outside the plan's scope (but see I4 for M3).
- The rest of the cold-vs-warm methodology (dropping VM caches, timing the first call): the ruling asks only that the limitation be stated, so the fix asked for in C1 is docs only.
- Whether 72/50 queries are statistically enough, or the consumer path having 5 queries x 1 run: the plan sets the sizes; it is labeled.
- The other containers in the VM during the run (two idle containers from an unrelated project, about 90 MiB): noted in the Task 9 report; negligible and outside the plan.
- The controller docs commits 1da1dbc and 1632c83 landing mid-run: docs and `.gitignore` only, disclosed in the results README.
- RDKit cartridge internals (whether `<%>` KNN is exact, GiST parallel safety): outside the parity contract in spec 8.4.

## Recommendations

1. Fix C1 (docs) before merge. Fix I1 (set `max_parallel_workers_per_gather=0`, or relabel consumer_path) before merge, since a published label is wrong.
2. Fix I2 and I3 together and regenerate the queries. Then rerun steps 5-10 at 500k so the published table rests on a count-checked, non-vacuous query set. This is cheap: about 3 hours of machine time, no reload.
3. Do I4 and I5 before starting M3. They decide whether the 1M load and build numbers mean anything.
4. Do I6, or record a ruling that defers it.
5. Take the Minors opportunistically. 2, 4 and 5 are small and protect the library and the gates.

## Assessment

**Ready to merge: With fixes.** The library change is sound and well tested, and the parity gates catch real mismatches. However, the published results mislabel "cold" (C1) and the consumer-path thread count (I1), and 8 dead queries plus an unchecked count path (I2, I3) make the parity backing thinner than the README implies. Fix the docs and labels, regenerate the queries and rerun steps 5-10, and harden the driver against stale state before M3.
