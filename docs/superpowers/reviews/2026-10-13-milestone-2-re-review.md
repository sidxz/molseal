### Finding Verdicts
- **C1 cold label** - ADDRESSED. docs/results/README.md "What cold means here", bench/README.md:131, README.md:147-152 state: restart + near-empty pg_buffercache (196 pages, matches timings_500000_cartridge_vm_cold.json), VM page cache not dropped, untimed count + warm-up, molseal cold == warm procedure, real cold cost cold_load_s 0.73/0.80 VM, 0.82 host (matches JSONs).
- **I1 3-core consumer_path** - ADDRESSED. bench/compose.yml adds -c max_parallel_workers_per_gather=0 (comment outside folded scalar); run_bench.py records SHOW value; both cartridge JSONs hold "0"; consumer_path now 39.2/39.0 s labeled threads=1; spec 8.2 updated.
- **I2 count_substructure unchecked** - ADDRESSED. parity.py check_substructure requires len(kept)==count==cartridge count; run_bench count rows take hits from the count call; parity_500000.json: all 72 gate2 rows have molseal==molseal_count==cartridge. test_check_substructure_counts_lists_and_rejected_rows covers a disagreeing count and a duplicate hit.
- **I3 dead BRICS queries** - ADDRESSED. queries.py strip_dummies zeroes radicals and SetNoImplicit(False) on attachment atoms before removal; fragments_from keeps a fragment only if its parent matches; parity zero_hit_queries is [] and min cartridge hits is 1. Two new tests (no radicals + parent match; hydrogen restore).
- **I4 stale state** - ADDRESSED. run_m2.sh: reset_volume runs only inside step3/step4, per service profile and explicit volume (rm -sf + docker volume rm bench_<vol>), so down -v cannot hit the other profile; step3 also removes progress, ids.tsv and snap; tenant_<N>.txt; step7 removes timings_<N>_*.json; STEPS list lets steps 2,5-10 reuse the loads (tenant_500000.txt kept, no reset outside 3/4). load_cartridge refuses non-empty mols or existing GiST indexes, creates indexes without IF NOT EXISTS, checks count(*)==n. run_bench.check_parity refuses missing/failed/older-than-load parity. Tests: test_check_parity_..., test_load_cartridge (own schema, second load refuses). Shell guards untested but the rerun exercised the non-reset path.
- **I5 chunk change skips rows** - ADDRESSED. load_molseal.py stores {rows_done, chunk}, skips by rows (islice(rows,done,done) verified to consume done items), refuses a mismatched chunk; test_resume_refuses_a_different_chunk_size asserts nothing loaded.
- **I6 statement-log leg** - ADDRESSED. docker-compose.yml command with MOLSEAL_LOG_STATEMENT; step2 force-recreates db with =all; gate.py capture_log, passes --log, requires log_statements>0, records log_hits (20042 / 0 per report). No positive control for the log leg; controller already ruled this goes to the M3 plan.
- **M1 function-attribute counter** - ADDRESSED. cli.py stats dict.
- **M2 mixed-tenant batch** - ADDRESSED. store.py:~211 ValueError before the transaction; test added. Cursor UPDATE and CAS untouched.
- **M3 put_many docstring** - ADDRESSED. vault.py docstring states per-chunk validation/atomicity, earlier chunks stay committed and indexed, exception carries no count; behaviour unchanged (loop still calls _put_chunk per group).
- **M4 retry path test** - ADDRESSED. test_put_many_retries_a_conflict_with_fresh_nonces_and_indexes_once asserts two attempts, disjoint nonces, add_batch once.
- **M5 parity pure functions** - ADDRESSED. check_substructure/check_similarity/overall_ok with tests; gate 3 records {"skipped": "count > 10000"} (27 skipped, 45 compared in data).
- **M6 p50** - ADDRESSED. median = statistics.median in summarize; p95 documented as nearest rank; test pins 2.5.
- **M7/M8/M9 docs wording** - ADDRESSED. results README "Other notes": host client via port forward (0.19 s p95), cartridge similar ids only, topk100 ungated, host values read by hand. GIT_SHA build arg deferred per ruling.
- **M10 2.4M estimate** - ADDRESSED. Published with basis, labeled not measured.
- **M11 CC BY-SA** - ADDRESSED. "Data licence" section in results README (root README attribution per report; not separately re-read).
- **M12 sidecar dupes** - ADDRESSED. Sidecar truncated to rows_done on start, flush after write; resume test asserts 25 unique lines.
- **M13 sampler** - ADDRESSED. sleep 5; effective ~7 s documented.
- **M14 .dockerignore** - ADDRESSED. *.key, .env, talks, snapshots added.

### New Breakage in the Fix Diff
None Critical or Important.
- Minor: bench/run_m2.sh step 3/4 `reset_volume` is destructive and unconditional on rerun of that step (intended; documented in the header). Resuming a crashed 500k load via STEP=3 now restarts from zero rather than resuming (progress file deleted), so the loader's resume feature is only reachable by running bench.load_molseal directly. Acceptable per ruling, worth a header note.
- Minor: gate.py parses the absence-check stdout as JSON (`json.loads(buf.getvalue())["log_hits"]`); it works today but breaks if the CLI ever prints extra stdout text.
- Minor: fragments_from asserts "matches its parent" only against the molecule being iterated for the first time a canon is seen (seen dict overwrites), which is sufficient for the guarantee but not recorded per query.
- Checked: no *.key, bench/data, bench/results or talks paths in tracked files or the diff stat; no consumer or competitor names in src/ or docs/results/README/bench README; tenants opaque; tag milestone-2 points at 6032468.

### Out-of-Scope Observations
- Statement-log leg has no planted positive control (already ruled for the M3 plan).
- Two molseal_vm timing JSONs record git SHA "unavailable (CalledProcessError)", disclosed in the results README.

### Verdict
**Fix round:** All findings addressed, no new Critical/Important breakage. Tests were not re-run; the report names covering tests (138 passed, 1 skipped; cartridge test alone passes) and the diff shows each new test exercises the fixed behaviour (I2, I4, I5 included). One empirical check run: islice skip semantics.
