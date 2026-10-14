# SDD ledger — plan: docs/superpowers/plans/2026-10-13-molseal-bench.md
Spec: docs/superpowers/specs/2026-10-13-molseal-poc-design.md sections 7, 8, 9, 12 (read, binding)
Branch: m2-bench, forked from main @607900f

Ruling: no per-task reviewer; one whole-branch review at the end — user's standing rule, same as milestone 1 — cost if wrong: late discovery of an early defect
Ruling: tasks 1-3 run before the Docker VM is resized (they only need the dev Postgres on :5439); execution pauses before Task 4 if `docker info` still reports under 16 GB — cost if wrong: none

## Preflight scan
| pair / task | produces -> consumes | finding |
|---|---|---|
| 1 -> 3 | Vault.put_many(items, chunk) ; SqlStore.put_structures/get_structures -> loader | signatures match; loader passes chunk=len(group) |
| 1 -> 1 | _advance(first, last=None) keeps put/delete callers (single arg) | ok |
| 3 -> 4,5,6 | load_molseal.row_id(chembl_id) -> cartridge loader, queries (source_id), parity | same function imported everywhere |
| 4 -> 6,7 | CARTRIDGE_URL, cartridge_rejected_<N>.txt, mols(id,chembl_id,smiles,m,mfp) with GiST | parity/run_bench read the same names |
| 5 -> 6,7 | queries_<N>.json {substructure:[{id,kind,query}], similarity:[{id,query,source_id}]} | both consumers index the same keys |
| 6 -> 7 | parity_<N>.json ok flag; cartridge_query_sql(kind) | run_bench refuses without ok=true; imports the helper |
| 7 -> 9 | run_bench CLI flags (--side/--mode/--where/--threads/--prewarm/--report) | driver uses exactly these |
| 8 -> 9 | compose services molseal-db (:5441), cartridge-db (:5440), bench; container name bench-cartridge-db-1 | driver and docker stats target match; name to be confirmed with docker ps |
| task 1 | put_structures executemany UPDATE loop for existing rows vs bulk INSERT for new | consistent with test expectations (seqs consecutive, live delta) |
| task 2 | download test must not hit the network | file pre-exists so download returns early |
| task 6 | `id = ANY(:ids)` UUID list binding; `<%>` KNN operator availability (task 7) | fallbacks written in the plan |
| task 4 | `-c rdkit.morgan_fp_size=2048` placeholder before CREATE EXTENSION | ALTER DATABASE fallback written in the plan |
Scan result: no contradictions with Global Constraints.

## Progress
Task 1: dispatched (base 607900f, model sonnet)
Task 1: complete (commits 607900f..e2bbce3; 110 passed with PG)
Task 2: dispatched (base e2bbce3, model sonnet)
Task 2: complete (commits e2bbce3..2928c7f; slices 10000/0 skipped and 499997/3 skipped)
Task 3: dispatched (base 2928c7f, model sonnet)
Task 3: complete (commits 2928c7f..68c501d; GATE OK on 10k: control in DB+dump, 0 hits, 19996 needles; leak r=0.89, predict 0.39 vs 0.38 baseline; 3377 rows/s)
Ruling: gate.py clears the stale sidecar and progress file before loading (implementer deviation, accepted: append-mode sidecar would duplicate lines) — cost if wrong: none
Ruling: VM still 9.7 GB; tasks 4-8 proceed (code + tiny integration tests only); execution stops before Task 9 unless docker info reports >= 15 GiB — cost if wrong: none for correctness, only timing fairness, which Task 9 guards
Task 4: dispatched (base 68c501d, model sonnet)
Task 4: complete (commits 68c501d..f9924b8; settings verified 4GB/2048/off, arch arm64)
Ruling: accepted deviations — POSTGRES_INITDB_ARGS UTF8 (image defaults to SQL_ASCII, psycopg3 fails on connect) and the INSERT binds :smi/:smic separately (AmbiguousParameter) — cost if wrong: none
Task 5: dispatched (base f9924b8, model sonnet)
Task 5: complete (commits f9924b8..f3fd441; 72 substructure + 50 similarity queries)
Task 6: dispatched (base f3fd441, model sonnet)
Task 6: complete (commits f3fd441..4df95ac; end-to-end parity deferred to Task 9 by design)
Task 7: dispatched (base 4df95ac, model sonnet)
Task 7: complete (commits 4df95ac..8dbc775)
Ruling: the brief interface line mentions --restart-cartridge and a capped parameter that its code does not have; the code is the authority (the driver restarts the container itself) — cost if wrong: none
Task 8: dispatched (base 8dbc775, model sonnet)
Task 8: complete (commits 8dbc775..ed56185; .dockerignore added to keep bench/data out of the image, accepted)
VM resized to 16 GB (user). Task 9: dispatched (base ed56185, model opus); memory sampling requested alongside
- 2026-10-13 17:55:42 Ruling: similarity index deferred — user agreed to revisit after the benchmark ladder (bit-count bands à la chemfp is the planned exact upgrade; no approximate index because parity must hold). Discussion saved in talks/.
- 2026-10-13 18:44:37 Ruling: consumer-driven search needs (FCFP family, Tversky, chiral Morgan, generalized substructure, get_many, fingerprints accessor) are library work and must stay generic; recorded in docs/backlog/2026-10-13-generic-search-surface.md for post-ladder planning.
- 2026-10-13 19:04:08 Task 9: complete (564cd7d, tag milestone-2, DONE_WITH_CONCERNS). Concerns: cold≈warm (page cache not dropped + untimed warm-up) → must be stated in results docs; stale cartridge volume kept old GiST/21 rows → driver must reset/check volume (fix wave); 30 s sampler misses short peaks (note); controller docs commits 1da1dbc/1632c83 landed mid-run (code under test = ed56185). Memory: cartridge 1.44 GiB, molseal VM rss 1117 MB, host rss 1470 MB; 2.4M est 6–7 GB cartridge / 5.5 GB molseal → fits 16 GB one side at a time.
- Final whole-branch review (opus) on 607900f..564cd7d: With fixes — 1 Critical, 6 Important, 14 Minor. Report: .superpowers/sdd/2026-10-13-molseal-bench/final-review.md. Tests: 127 passed, 1 skipped with the PG URL set.
- Rulings on the final review (one fix wave, BASE 564cd7d, model opus):
  - C1 fix (docs): state what "cold" means (restart + near-empty pg_buffercache; VM page cache not dropped; untimed count + warm-up before timed repeats; molseal cold == warm procedure, real cold cost = cold_load_s 0.69 s VM / 0.64 s host). Keep the molseal cold rows, labeled as same-procedure.
  - I1 fix: max_parallel_workers_per_gather=0 in bench/compose.yml cartridge command; rerun timing so consumer_path is a true single backend.
  - I2 fix: parity gate 2 compares count_substructure to SELECT count(*) and to list-length of hits; count timing rows take hits from the count call.
  - I3 fix: BRICS fragments lose their radicals (SetNumRadicalElectrons(0)/SetNoImplicit(False) on attachment atoms); assert each fragment matches its parent; record per-query hits; regenerate queries.
  - I4 fix: steps 3/4 reset their volumes (down -v) and delete <slice>.progress.json/.ids.tsv; tenant per N (tenant_<N>.txt; rename the existing tenant.txt for 500k); clear snap dir at step 3; load_cartridge asserts count(*)==n-rejected and fails if the GiST indexes pre-exist; run_bench refuses a parity file older than the newest load_*_<N>.json; step 7 removes timings_<N>_*.json before timing so --report sees only this run. Guards must NOT touch the existing 500k load when resuming at STEP>=5.
  - I5 fix: progress = {rows_done, chunk}; refuse a mismatched --chunk; test.
  - I6 fix: step 2 starts db with log_statement=all, captures `docker compose logs db`, passes --log; gate_10k.json gains log_hits.
  - Minors taken: 1, 2, 3 (docstring), 4, 5, 6 (statistics.median for p50), 7/8/9 (docs wording only), 10 (publish the 2.4M estimate with basis), 11 (CC BY-SA line for queries_*.json), 12, 13 (5 s sampler), 14.
  - Deferred (no code): 9's GIT_SHA build arg (docs say the values are read from the host by hand); M1 minor 13 (_pack via ToBitString) → profile before the 2.4M run.
  - After the fixes: rerun step 2 (10k gate with the log leg) and steps 5–10 at 500k WITHOUT reloading (the 500k loads passed parity; the guards must leave them in place), republish docs/results/2026-10-13-500k, move the milestone-2 tag to the new head. Why: 8 dead queries + unchecked count + 3-core consumer path mean the published 500k table is not yet the one to carry into M3; machine cost ~3 h, no reload.

## Fix wave hand-back (2026-10-13 22:3x)
- Status DONE_WITH_CONCERNS. Commits 64ea5ae (lib), 70730af (parity/queries/p50), 7f8db75 (driver/statement log/stale state), 6032468 (republish 500k; tag milestone-2 moved here). pytest 138 passed, 1 skipped (cartridge test passes alone on :5440).
- Rerun STEPS="2 5 6 7 8 9 10" 19:19–22:28 exit 0; loads reused. Gate: 20042 statements logged, 0 hits. Parity: no zero-hit queries. consumer_path 39.2 s cold / 39.0 s warm on one backend.
- Report: fix-wave-report.md.
- Rulings on the implementer's concerns:
  - Idle bench DBs during step 2: no effect on an absence check — accepted.
  - Statement-log leg has no planted positive control: real gap; a control statement with a known SMILES literal that the gate must find exactly once goes into the M3 (1M) plan, not a rerun now — costs one more gate change if the grep were broken.
  - `rm -sf` + `docker volume rm` instead of `down -v`: correct under profiles — accepted.
  - Cartridge loader checks count(*) == n with rejected rows stored as NULL mol: consistent with how rejects are kept — accepted.
  - Port-forward overhead published as the measured 0.19 s, not the review's 0.14 s: accepted.
- Next: scoped re-review of 564cd7d..6032468, then adjudicate residuals, delete workspace when clean, finishing-a-development-branch.

## Scoped re-review of the fix wave (564cd7d..6032468)
- Verdict: all findings addressed (C1, I1–I6, minors 1–14 taken), no new Critical/Important breakage. Report: re-review-fix-wave.md.
- Residuals, all parked (no further fix dispatch; carried into the M3 plan):
  - Ruling: STEP=3 rerun reloads from zero; the loader's resume is reachable only by calling bench.load_molseal directly — accepted as designed; the M3 plan says so in the run_m2.sh header — costs one confused rerun if forgotten.
  - Ruling: gate.py parses the absence-check CLI stdout as JSON — fine while the CLI prints JSON only; M3 routes the gate through the function instead — costs a gate crash, never a false pass.
  - Ruling: fragments_from checks the parent match against the parent that produced the fragment, not recorded per query — enough for the guarantee; no action.
  - Ruling: two molseal VM timing JSONs record the git SHA as unavailable (no git in the bench image) — disclosed in the results README; GIT_SHA build arg stays deferred.
  - Ruling: statement-log leg has no planted positive control — required gate change in the M3 plan (already ruled at hand-back).
- Final review clean → review record preserved in docs/superpowers/reviews, workspace deleted, finishing-a-development-branch.
