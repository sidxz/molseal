# SDD ledger — plan: docs/superpowers/plans/2026-10-13-molseal-library.md
Spec: docs/superpowers/specs/2026-10-13-molseal-poc-design.md (read, binding authority)
Branch: milestone-1, forked from main @72dd519

Ruling: no per-task reviewer; one whole-branch review at the end — user's standing rule (2026-08-25, confirmed in the 2026-10-13 handoff) overrides the skill's per-task gate — cost if wrong: an early-task defect is caught only at the end and reworked across later tasks
Ruling: work on branch milestone-1 inside the repo, no separate worktree — the repo has no other activity — cost if wrong: none practical
Ruling: Task 10 test `test_no_snapshot_dir_means_save_fails_loudly` asserts `VaultError`, not bare `Exception` — the plan's broad assert would pass on any crash — cost if wrong: none

## Preflight scan
| pair / task | produces -> consumes | finding |
|---|---|---|
| 2 -> 3 | crypto.seal/open_sealed/key_aad -> keys.wrap_key/unwrap_key | names and arg order match |
| 2 -> 9 | crypto.seal_chunks/open_chunks/CHUNK_SIZE/NONCE_LEN -> snapshot | match |
| 2 -> 10 | crypto.pad/unpad/seal/open_sealed/row_aad -> vault | match |
| 3 -> 10 | keys.TenantKeys/wrap_key/unwrap_key/exact_key_hmac/new_tenant_secret -> vault | match; vault expects SealError on wrong KEK (test), keys raises crypto.SealError: ok |
| 4 -> 10 | payload.Payload/encode/decode -> vault | match |
| 5 -> 6 | store tables + SqlStore -> PG test uses tenants/structures tables directly | match |
| 5 -> 10 | SqlStore.put_structure(rec, key_hmacs)->int, seq_cursor, find_by_key, changes_since, live_count, get_structure, ensure_tenant, get_tenant_keys, put_tenant_key -> vault | all present in Protocol and impl |
| 5 -> 12 | store tables -> cli absence-check raw insert/select/delete | match; control row seq=0 never read by vault (changes_since uses seq > last) |
| 7 -> 8 | StructureIndex internals (_n/_live/_fps/_pop/_ids/_pos_of/_row_versions/_order_dirty) -> mark_dead/similar/to_parts/from_parts | names match; from_parts calls _ensure_capacity then sets _n |
| 8 -> 9 | to_parts/from_parts dict keys meta,lib,ids,live,rv,fps -> snapshot.PART_ORDER | match |
| 9 -> 10 | snapshot.save(index,path,key,tenant,kv)/load(path,key,tenant,kv) -> vault | match |
| 10 -> 11 | Vault API (put/delete/get/find_exact/substructure/count_substructure/similar/save_snapshot) -> cli | match |
| 11 -> 12 | cli _store/_vault/_add_common/build_parser -> absence-check/leak-report | match |
| task 1 | pyproject + scaffold test | self-consistent; risk: rdkit 2025.3.3 wheel resolution on py3.13 arm64 (verified available) |
| task 7 | RDKit API: GetMatches/CountMatches keyword names, AddPatterns(numThreads=), GetFingerprints(numThreads=) | reviewer verified on 2025.03.3; positional fallback written in the plan |
| task 7 | ring SMARTS on trusted holder | reviewer saw parity; plan names CachedSmilesMolHolder fallback |
| task 8 | SetSearchOrder([]) when all positions dead; CountMatches honoring search order | guarded by len(self)==0 early return; CountMatches fallback written in the plan |
| task 8 | SubstructLibrary(bytes) constructor | reviewer verified; report-not-improvise instruction present |
| task 10 | pytest.raises(Exception) in one test | ruled above: use VaultError |
| task 12 | pg_dump URL conversion only handles postgresql+psycopg:// | acceptable for POC; other drivers fall through to pg_dump's own error |
Scan result: no contradictions between tasks or with Global Constraints.

## Progress
Task 1: dispatched (base 72dd519, model sonnet)
Task 1: complete (commits 72dd519..0dbe0f9, no per-task review per ruling)
Task 2: dispatched (base 0dbe0f9, model haiku)
Task 2: complete (commits 0dbe0f9..3dc1d86)
Task 3: dispatched (base 3dc1d86, model haiku)
Task 3: complete (commits 3dc1d86..d84dd69)
Task 4: dispatched (base d84dd69, model haiku)
Task 4: complete (commits d84dd69..afea0de)
Task 5: dispatched (base afea0de, model sonnet)
Task 5: complete (commits afea0de..ed73355)
Ruling: dev Postgres moved from port 5433 to 5439 — 5432-5437 are held by other local projects; compose, plan and briefs 6/12/13 updated in commit 2a3803c — cost if wrong: none
Task 6: dispatched (base 2a3803c, model sonnet)
Task 6: complete (commits 2a3803c..accdf4b; PG sequencing proof green)
Task 7: dispatched (base accdf4b, model sonnet)
Task 7: complete (commits accdf4b..46dbf35; 438 molecules in oracle set, no API fallbacks)
Task 8: dispatched (base 46dbf35, model sonnet)
Task 8: complete (commits 46dbf35..a15de4c; no API fallbacks)
Task 9: dispatched (base a15de4c, model haiku)
Task 9: complete (commits a15de4c..b20a5fa; trailer amended to the required line by controller)
Task 10: dispatched (base b20a5fa, model sonnet)
Task 10: complete (commits 2aff509..71ad5d2)
Task 11: dispatched (base 71ad5d2, model sonnet)
Task 11: BLOCKED round 1 — plan test bug: test_load_search_count_similar never drains capsys after the [r5] search, so the similar assertion reads the wrong output. Ruling: add `capsys.readouterr()` after the [r5] search line; CLI code unchanged — cost if wrong: none (test-only)
Task 11: complete (commits 71ad5d2..c4ee34f; plan test fixed in 8d9982a)
Task 12: dispatched (base 8d9982a, model sonnet)
Task 12: complete (commits 8d9982a..f62aa34; PG absence check ok=true, control_found=true, db_hits=0, 844 needles, 32 short skipped)
Task 12: minor (deferred): `absence-check --run-pg-dump` exits 0 with only a stderr notice when pg_dump is missing; arguably should fail the run or set ok=false
Task 12: minor (deferred): pg_dump not installed on this Mac; bench plan should run the dump scan via `docker compose exec db pg_dump`
Task 12: note: leak report degenerate on the 438 dev molecules (all in size class 256, r=0.0); expected to spread at ChEMBL scale
Task 13: dispatched (base f62aa34, model sonnet)
Task 13: complete (commits f62aa34..44f9da1; tag milestone-1; 86 passed with PG, 84 passed 2 skipped without)
Final review: dispatching whole-branch review over 72dd519..44f9da1 (model opus)
Final review: 1 Critical, 6 Important, 14 Minor (final-review.md). Verdict: merge with fixes.
Ruling C1: absence check must scan needles in raw AND hex form (bytea renders as hex in pg_dump and statement logs); the control row must be found in the DB leg and in any dump leg; `--run-pg-dump` with pg_dump missing fails the run (ok=false, exit 1); the log leg carries no control (it was captured before the check ran) and the README says so — cost if wrong: a false "clean" on the write-up's central demo
Ruling I1: `put_structure` takes `expected_row_version` (None = must not exist) and raises `VersionConflict` inside the transaction, after the cursor lock; vault.put/delete retry on conflict — cost if wrong: lost update between two processes
Ruling I2: `tenants` carries `live_count` maintained in the write transaction; `changes_since` gains an `upto` bound; the vault reads (seq_cursor, live_count) in one statement, catches up to that cursor, then compares — cost if wrong: spurious rebuilds under the lock
Ruling I3: search order applied lazily once before a search (`_ensure_order`), never per tombstone or per put — cost if wrong: O(n) per write at scale
Ruling I4: keys are created only for a tenant with no structures (max_seq == 0) and both key rows insert in one transaction; otherwise VaultError — cost if wrong: unrecoverable data on a key-row loss
Ruling I5: `canonicalize` re-parses its own output and raises StructureError if that fails; engine keeps an `unindexed` id set for rows it had to skip and the vault's live-count comparison adds it — cost if wrong: refresh rebuild loop
Ruling I6: the 10k absence run moves to milestone 2's first step (the ChEMBL loader is the only 10k data source); this fix wave re-verifies on the 438-molecule set with a real `pg_dump` taken via `docker compose exec` — cost if wrong: a leak class only visible at scale surfaces one milestone later
## Final review minors (deferred, from final-review.md)
## Final review minors (deferred, from final-review.md lines 74-90)
Final: minor (deferred): 1. **`refresh()` and the constructor do not check for sequence regression; only searches do.** `vault.py:43`, `169-171`. Spec 6.5 puts the regression check in `
Final: minor (deferred): 2. **README overstates regression detection.** `README.md:27` says "The engine detects sequence regression and rebuilds". It detects the cursor going below `las
Final: minor (deferred): 3. **A bad snapshot crashes `Vault.__init__`.** `vault.py:71-75`. A snapshot is a cache. A corrupt, foreign or half-written file should be logged and set aside,
Final: minor (deferred): 4. **The cap-honesty test cannot fail.** `tests/test_engine.py:149-158` uses `max_results=5` with 3 live rows, so dead positions consuming the cap would not cha
Final: minor (deferred): 5. **`similar(top_k=-1)` returns n-1 results.** Verified: 4 of 5. `engine.py:204`. Validate `top_k >= 0`, `max_results >= 0` and `0 <= threshold <= 1` at the fa
Final: minor (deferred): 6. **The absence check passes vacuously with zero needles** (an empty or all-short sidecar, `cli.py:147,176`). Require `needles > 0`. Also, a crash between plan
Final: minor (deferred): 7. **`pg_dump --dbname <url>` puts the password in argv** (visible in `ps`), and only `+psycopg` URLs are converted (`cli.py:164-165`). Pass the password throug
Final: minor (deferred): 8. **`leak-report` crashes on an empty intersection** (`np.bincount` of an empty array, `cli.py:190-202`), and its accuracy is in-sample. Report "n=0" and label
Final: minor (deferred): 9. **Spec test-list gaps.** The store suite runs only on SQLite (spec 7 says SQLite and PostgreSQL; parametrize the `store` fixture). The PostgreSQL concurrency
Final: minor (deferred): 10. **`exact_key_hmac` framing is ambiguous if `kind` contains NUL** (`keys.py:74`). Reject NUL in `kind` and enforce the 64-character column limit before the w
Final: minor (deferred): 11. **Deleting an already-deleted id writes another tombstone** (`vault.py:111-122`), bumping the cursor and `row_version`. Most callers would expect `NotFound`
Final: minor (deferred): 12. **Rebuild runs while holding the lock** (`vault.py:207-210`). Spec 6.5 says "builds a fresh index aside and swaps it in under the lock". Building outside th
Final: minor (deferred): 13. **`_pack` goes through `ToBitString`** (`engine.py:45-47`), which allocates a 2048-character string per molecule; at 2.4M that is noticeable at ingest. `rdF
Final: minor (deferred): 14. **One tampered row stops every search for the tenant.** `_catch_up` lets `SealError` escape. This is consistent with "rejected", but reporting and skipping 
Final fix wave: complete (commits 44f9da1..e1e284f; 101 passed with PG, 98 passed 3 skipped without; I6 re-run ok=true, control found in DB and dump legs, 0 hits)
Ruling: the absence-check control row persists as a per-tenant seq-0 tombstone (never read by a vault, not counted live) so a dump taken before the check contains it; README documents plant-then-dump — cost if wrong: one known plaintext marker row per tenant, carrying no structure
Ruling: fix-wave commit trailers rewritten to the required line via filter-branch (local branch, never pushed) — cost if wrong: none
Ruling: tag milestone-1 moves to the reviewed head once the re-review is clean — cost if wrong: none
Note: tenants.live_count is new; fresh databases get it from create_all; the dev DB on :5439 was altered by hand (no migration tooling in the POC)
Final re-review: all 7 findings ADDRESSED, no new Critical/Important breakage; control row judged safe
Final: minor (deferred): live count read at cursor C and changes_since in separate statements; a concurrent update moving a row above C causes one spurious bounded rebuild (performance only)
Final: note: existing databases need `ALTER TABLE tenants ADD COLUMN live_count bigint NOT NULL DEFAULT 0` + backfill; no migration tooling in the POC
Ruling: review record (final review + ledger) copied into docs/superpowers/reviews before the workspace is deleted — milestone 2 needs the deferred-minor list — cost if wrong: none
