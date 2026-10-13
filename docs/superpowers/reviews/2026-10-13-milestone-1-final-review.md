# molseal milestone 1: whole-branch review

Range: 72dd519..44f9da1 (branch `milestone-1`). Reviewed in one pass over every module under `src/molseal`, every test, the README, the spec, the plan's Global Constraints and Review Focus, and the ledger. Suite re-run: 86 passed with `MOLSEAL_TEST_DATABASE_URL` set. Findings marked "verified" were reproduced with a scratch script (outside the repo) against the branch head; nothing in the working tree was modified.

## Strengths

- **Boundaries are clean and the generic-library rule holds.** No SQL in `engine.py`, no chemistry in `store.py`, `vault.py` is the only module that joins them. A grep for consumer terms and InChI under `src/molseal` hits only `cli.py`, which acts as a caller and says so in its docstring.
- **The cursor-row sequencing is right, and a real PostgreSQL test pins it.** `tests/test_store_pg.py:16` holds writer A's cursor lock, proves writer B blocks, proves a reader sees nothing uncommitted, and proves the resulting seq order equals commit order. This is the hard part of spec 4 and it is tested properly.
- **AAD construction is careful.** Row AAD (40 bytes), key-wrap AAD and snapshot-chunk AAD (48 + 8 bytes, prefixed with `MOLSEAL1`) are fixed width with distinct lengths, so the data key's two uses cannot be confused. The snapshot AAD binds tenant, key version, format version, `last_seq`, chunk index and chunk count, and the tests cover header edit, chunk tamper, truncation, reorder and drop (`tests/test_snapshot.py:46`).
- **The payload is authoritative.** Tombstones and exact keys live inside the sealed payload. `find_exact` decrypts each candidate and checks the key (`vault.py:145`), and a test copies an HMAC row onto another id to prove it (`tests/test_vault.py:58`).
- **The dead mask works, including caps.** Verified: with 6 positions, the first 3 dead, `substructure(max_results=2)` returns exactly 2 live ids, and `count_substructure` returns 3. The position filter at `engine.py:158` is a sound extra guard. The oracle tests compare against brute-force RDKit on 438 molecules, including ring SMARTS and a chiral query.
- **Snapshot rollback does no harm by design.** An older but valid snapshot simply catches up through `changes_since`, so swapping in an old snapshot file cannot make the index wrong.
- **Review Focus items 1, 2, 4 and 5 are pinned by direct tests** (`test_unparseable_smiles_writes_nothing`, pad boundary `n=252 -> 256`, the empty-state tests, snapshot tenant/key-version/header refusal). Item 3 is pinned by the edit test; its cap half is pinned only weakly (see Minor 4).
- Key file creation uses `O_CREAT|O_EXCL` with mode 0600, and the provider refuses group or world bits.

## Issues

### Critical (Must Fix)

**C1. The absence check cannot see a leak in a dump or statement log. Its `--dump` and `--log` legs pass with nothing checked, and `--run-pg-dump` either cannot pass or passes when it never ran.**
`src/molseal/cli.py:139-179`.

- Every column that could hold a leaked structure (`ciphertext`, `nonce`, `wrapped`, `hmac`) is `bytea`. `pg_dump -Fp` writes bytea in COPY text form as hex: verified on the dev Postgres, `COPY (SELECT 'MOLSEAL-CONTROL'::bytea) TO STDOUT` emits `\\x4d4f4c5345414c2d434f4e54524f4c`. A statement log written with `log_statement=all` renders bytea parameters through the same output function. The scanner searches for raw ASCII needles (`_hits`, line 139), so a plaintext SMILES written by mistake into any of these columns would show up in the dump and the log as hex, and the scan would report 0 hits.
- The control row only proves the column scan. It is never looked for in a `--dump` file (lines 168-170) or in a `--log` file (line 174). Both files are captured before the command runs, so the control could not be in them anyway. The result is that `--dump some.dump` (even a compressed custom-format dump, the exact case spec 9.2 warns about) or `--log wrong.log` reports `ok: true`.
- `--run-pg-dump` does check for the control (line 167), so on a real dump it will always report `control_found: false` and `ok: false`, because the control is hex in the dump. The documented procedure therefore cannot pass. When `pg_dump` is missing it does the opposite: it prints to stderr, leaves `dump_hits=None`, and `not None` makes `ok` true (the ledger's deferred minor).
- `test_absence_check_catches_a_plaintext_log` (`tests/test_cli.py:68`) hand-writes a log with the needle as an ASCII SQL literal, which is not how PostgreSQL logs a bytea parameter. The test passes for a reason that does not hold in production.

**Why it matters:** "Absence" is claim 1 of the POC (spec 1, 9) and the milestone-1 gate. As built, the tool can only prove the column scan, which reads straight back the bytes it wrote. The dump and log legs are the parts that make the claim worth publishing.

**Fix:** (a) Search each needle in three encodings: raw, lowercase hex, and the COPY-escaped `\\x` hex form. Lowercase hex alone covers both hex forms, but a needle can start at an odd nibble boundary inside a bytea value, so also search the hex of each needle at both alignments, or decode every `\\x[0-9a-f]+` run in the dump and log back to bytes before scanning (simpler and exact). (b) Make each leg falsifiable on its own. Plant the control row before the dump or log window: for `--run-pg-dump`, plant then dump; for `--log`, plant during the captured window and require the control in the log. Report `control_found` per source. (c) Any requested source that did not run (pg_dump missing, file unreadable) sets `ok=false`. (d) Replace the log test with one that runs through a real driver-parameterized insert, or at least writes the needle in PostgreSQL's hex parameter form.

### Important (Should Fix)

**I1. Concurrent writes to one id can reuse a `row_version`, and the losing process then serves the wrong structure forever.** Verified.
`src/molseal/vault.py:102-106` (and `111-119` for delete), `src/molseal/engine.py:89-90`, `src/molseal/store.py:142-163`.

`put` reads `row_version` outside the store transaction, then writes `rv+1`. Two vaults (two processes, or two instances in one process) that read the same version both write the same `rv`, and the last commit wins in the database. The engine deduplicates on `row_version` (`has()`), so the process that lost the race sees the winner's row arrive at a version it already holds and skips it. Reproduced: after v1 writes benzene at rv 2 and v2 (with a stale read) writes CCCl at rv 2, the database holds CCCl, v1's `substructure("CCCl")` is `[]`, and v1's `substructure("c1ccccc1")` returns the id. The live count still matches, so no rebuild ever repairs it. The same race on a brand-new id gives two writers rv 1. It also means two different ciphertexts carry an identical AAD, which weakens the "rollback of a row" story, since versions no longer name one state.

Spec 6.5 says multi-process deployments get consistency through the cursor check. This breaks that silently and permanently.

**Fix:** The cursor row lock already serializes every writer for the tenant, so do a compare-and-set inside `put_structure`: take the cursor, then `UPDATE structures ... WHERE row_version = :rv - 1` (or insert when `rv == 1`, failing on conflict). If no row matches, roll back and raise a `Conflict`; `Vault.put`/`delete` re-read, re-seal and retry. As belt and braces, key the engine's freshness check on `seq` (unique per write) rather than `row_version`. Add a test that reproduces the stale read. This changes the store protocol that milestone 2 builds on, so do it now.

**I2. The live-count consistency check races with concurrent writers, which triggers false full rebuilds under the lock, and it costs a `count(*)` on every refresh.**
`src/molseal/vault.py:201-205`, `src/molseal/store.py:183-187`.

`_catch_up` reads changes and `live_count` reads the count in separate statements. Any commit from another process that lands between them (an insert raises the count, a delete lowers it) makes the counts disagree, and the vault rebuilds the entire index while holding the only lock. At full ChEMBL the spec estimates a rebuild at about a minute, during which every search blocks. With several writer processes this will fire routinely. Separately, `SELECT count(*) ... WHERE deleted = false` has no covering index, so on 2.4M rows each refresh pays a scan; the end-to-end timings in milestone 2 will include it.

**Fix:** Read the cursor and the live count atomically, and compare only at that cursor. Simplest: keep `live_count` in the `tenants` row, maintained inside `put_structure` (with +1, -1 or 0 by transition, under the cursor lock). Read `(seq_cursor, live_count)` in one select, catch up to exactly that cursor, and compare. That removes both the race and the scan. If you keep `count(*)`, run both reads in one REPEATABLE READ transaction.

**I3. The search order is rebuilt over every position on each tombstone, and on each put once any row has died. That cost is linear in index size, multiplied by the number of operations.**
`src/molseal/engine.py:102-106`, called from `add_batch` (line 137) and from `mark_dead` (line 181).

Measured: building the order list for 2.4M positions takes about 52 ms with the list comprehension (17 ms with `.tolist()`). `SetSearchOrder` costs about 4 ms per 300k positions, roughly 35 ms at 2.4M. `_catch_up` calls `mark_dead` once per tombstone, so a refresh batch of 10k deletes costs 10k × about 90 ms, about 15 minutes, all under the lock. Every `Vault.put` or `delete` after the first deletion pays about 90 ms at full scale. This will distort milestone 2's write and refresh numbers and makes a delete-heavy catch-up impractical at the spec's stated scale.

**Fix:** Make the order lazy: `mark_dead` and `add_batch` only set `_order_stale = True`, and `substructure` and `count_substructure` call `_apply_order()` when the flag is set. Use `np.flatnonzero(...).tolist()`. Then a refresh applies the order once. Keep the `len(self) == 0` guard: verified that `SetSearchOrder([])` makes RDKit search every position (all-dead index, `CountMatches` returns 1), so an empty order must never reach a search.

**I4. If a tenant's key rows are missing, the vault mints new tenant keys over existing data.** Verified.
`src/molseal/vault.py:47-56`.

`if not wrapped:` treats "no key rows" as "new tenant". After deleting `tenant_keys` for a tenant that has structures, `Vault(...)` writes two fresh wrapped keys and then fails with `SealError` on refresh. The tenant now holds key rows that open none of its data, and restoring the original rows from backup conflicts on the primary key. A partial restore or an operator mistake can trigger this, not only an attacker. The two key inserts are also separate transactions (lines 53 and 55): a crash between them leaves a tenant that raises "key rows are missing" forever.

**Fix:** Mint keys only when `ensure_tenant` created the tenant (return a `created` flag) or when the tenant has no structure rows and `seq_cursor == 0`. Otherwise raise `VaultError`. Insert both wrapped keys in one transaction, for example with a `put_tenant_keys(tenant, [data, hmac])` store method.

**I5. A SMILES that RDKit cannot re-parse is dropped silently. The id keeps its old structure live, and every later refresh rebuilds the whole index.**
`src/molseal/engine.py:33-37`, `engine.py:116-119`, `vault.py:203-204`.

`canonicalize` checks that the input parses, not that its canonical output re-parses. When canonical output fails to re-parse (rare, but RDKit has had such cases with unusual aromatic and charged systems, and at 2.4M rows "rare" is not zero), `add_batch` drops the row with no log (spec 6.2 asks for a logged count). For an edit, `row_versions` is not advanced and the old position stays live, so the old structure is served. After that, `live_count != len(index)` on every refresh, the vault rebuilds, the rebuild drops the same row, and the cycle repeats.

**Fix:** In `canonicalize`, re-parse the output and raise `StructureError` if it fails, so `put` rejects the row before any write (this extends Review Focus 1). In `add_batch`, for a row that does not parse, still mark the old position dead and record the version, and return or log the dropped count.

**I6. The milestone-1 gate was run on 438 molecules, not the 10k slice the spec names, and without the dump or log legs.**
Spec 14, milestone 1: "absence check with negative control passing on a 10k slice". The ledger records 844 needles from the dev set, with no dump and no log. Given C1, the dump and log legs would not have shown anything even if they had run. **Please confirm** whether the plan intentionally deferred the 10k slice to the benchmark loader. If it did, record a ruling. Otherwise rerun after C1 is fixed, with `docker compose exec db pg_dump` and a real `log_statement=all` capture.

### Minor (Nice to Have)

1. **`refresh()` and the constructor do not check for sequence regression; only searches do.** `vault.py:43`, `169-171`. Spec 6.5 puts the regression check in `refresh()`. A snapshot ahead of a restored database is kept, and `len(vault)` is stale, until the first search. Fix: have `refresh()` and `__init__` call `_sync_locked`. `store.max_seq` is then unused (`store.py:178`); either use it, as spec 6.5 describes, or drop it from the protocol.
2. **README overstates regression detection.** `README.md:27` says "The engine detects sequence regression and rebuilds". It detects the cursor going below `last_seq` at search time. A restore followed by enough new writes to overtake `last_seq` goes undetected unless the live counts happen to differ. Say so.
3. **A bad snapshot crashes `Vault.__init__`.** `vault.py:71-75`. A snapshot is a cache. A corrupt, foreign or half-written file should be logged and set aside, and the index rebuilt from the store. "Rejected" should not mean "the tenant cannot start". Related: `snapshot.py:35` uses a fixed `.tmp` name, so two processes saving concurrently can interleave. Use `tempfile.NamedTemporaryFile(dir=..., delete=False)`.
4. **The cap-honesty test cannot fail.** `tests/test_engine.py:149-158` uses `max_results=5` with 3 live rows, so dead positions consuming the cap would not change the result. Put the dead positions first and cap below the live count (verified that the code passes this).
5. **`similar(top_k=-1)` returns n-1 results.** Verified: 4 of 5. `engine.py:204`. Validate `top_k >= 0`, `max_results >= 0` and `0 <= threshold <= 1` at the facade.
6. **The absence check passes vacuously with zero needles** (an empty or all-short sidecar, `cli.py:147,176`). Require `needles > 0`. Also, a crash between planting and removing the control leaves a row whose fixed id makes every later run fail on the primary key (`cli.py:151`). Delete any stale control row before inserting.
7. **`pg_dump --dbname <url>` puts the password in argv** (visible in `ps`), and only `+psycopg` URLs are converted (`cli.py:164-165`). Pass the password through `PGPASSWORD` in `env=`, and use `sa.engine.make_url(...).set(drivername="postgresql")`.
8. **`leak-report` crashes on an empty intersection** (`np.bincount` of an empty array, `cli.py:190-202`), and its accuracy is in-sample. Report "n=0" and label the accuracy as an in-sample upper bound.
9. **Spec test-list gaps.** The store suite runs only on SQLite (spec 7 says SQLite and PostgreSQL; parametrize the `store` fixture). The PostgreSQL concurrency test does not show "the engine ingests both" (spec 10). The rollback-triggers-rebuild test runs only on SQLite. There is no row-level "fresh nonce on update" assertion; `test_every_seal_uses_a_fresh_nonce` is generic, so add one `put` / `put` / compare-nonces test.
10. **`exact_key_hmac` framing is ambiguous if `kind` contains NUL** (`keys.py:74`). Reject NUL in `kind` and enforce the 64-character column limit before the write, rather than failing inside the transaction.
11. **Deleting an already-deleted id writes another tombstone** (`vault.py:111-122`), bumping the cursor and `row_version`. Most callers would expect `NotFound`, or a no-op.
12. **Rebuild runs while holding the lock** (`vault.py:207-210`). Spec 6.5 says "builds a fresh index aside and swaps it in under the lock". Building outside the lock, with a final catch-up under it, would keep searches available during the roughly one-minute rebuild. This matters once I2 is fixed and rebuilds are rare but long.
13. **`_pack` goes through `ToBitString`** (`engine.py:45-47`), which allocates a 2048-character string per molecule; at 2.4M that is noticeable at ingest. `rdFingerprintGenerator`'s `GetFingerprintAsNumPy`, or `DataStructs.ConvertToNumpyArray`, avoids it. Also note for the milestone-2 fingerprint parity check: `np.packbits` is MSB-first per byte, while the cartridge's binary form is LSB-first, so compare bit sets, not raw bytes.
14. **One tampered row stops every search for the tenant.** `_catch_up` lets `SealError` escape. This is consistent with "rejected", but reporting and skipping the row (and excluding it from the live-count comparison) would serve a reasonable operator better. Recorded here for completeness; see Declined.

## Triage of the ledger's deferred minors

| Ledger item | Ruling |
|---|---|
| Task 12: `absence-check --run-pg-dump` exits 0 with only a stderr notice when pg_dump is missing | **Must fix before merge.** It is part of C1: a requested leg that did not run must set `ok=false`. |
| Task 12: pg_dump not installed on the Mac; the bench should run the dump via `docker compose exec db pg_dump` | **Can wait** for the benchmark plan. Pin pg_dump 16 so it matches the server. Note that the hex-decoding fix in C1 is needed whichever way the dump is produced. |
| Task 12 note: leak report degenerate on 438 dev molecules (all size class 256, r=0.0) | **Can wait** for milestone 2 (ChEMBL slice). The spec requires the two numbers in the README verbatim; leave the README placeholder until then. |

## Declined to judge

- Benchmark harness, parity contract and timings (spec 8): a separate plan, not in this range.
- Key rotation, KMS providers, async API, readers-writer lock, multi-process invalidation, molfiles, stereo-aware and tautomer-insensitive search: out of scope per spec 13.
- Serial SMILES parsing instead of `MultithreadedSmilesMolSupplier` (`engine.py:41`) and whole-file snapshot loading (`snapshot.py:48`): deliberate, marked `ponytail:` shortcuts, to be measured in milestone 2.
- Transient memory during snapshot save and load (about 3x the index): the spec acknowledges transient copies; milestone 2 measures them.
- Availability against a write attacker (flipping the `deleted` column to force rebuilds or hide rows from `find_exact`, tampering rows to stop refresh): spec 2 promises rejection and integrity, not availability, and a write attacker can delete rows anyway. Minor 14 is noted but not graded above Minor.
- Plaintext structures in process memory, swap and core dumps: the trusted side of the spec 2 threat model.
- The CLI's plaintext sidecar file next to the source SMILES: a test-harness artifact derived from an already-plaintext input.
- Leakage of the number of exact keys per row and of `kind` names (plaintext column): metadata the spec's leak list covers in spirit (row-level structure of the index); not separately listed, and a caller choosing kinds controls it.
- The university open-source policy check (spec 11): not a code matter.

## Recommendations

1. Fix C1 first. It is the POC's headline claim. Then rerun the gate on a 10k slice with a real dump and log (I6).
2. Fix I1 and I2 together, since both change `put_structure` and the tenants row: compare-and-set on `row_version` plus a transactional `live_count` in `tenants`. Do it before milestone 2 freezes the store interface the benchmark builds on.
3. I3 is a small change (a lazy order flag) with a large effect on milestone-2 write and refresh numbers. Do it before benchmarking.
4. I4 and I5 are short, local fixes with tests.
5. The plan, not just the executor, carried I1, I2 and I3: the vault and engine code match the plan's listings. Future plans in this repo should state the concurrency assumption for each read-modify-write, and the per-operation cost at the spec's scale.

## Assessment

**Ready to merge?** With fixes

**Reasoning:** The cryptography, sequencing and dead-mask core is sound and well tested. However, the absence check, the deliverable that proves claim 1, cannot detect a leak in the dump or log it is meant to scan. And a multi-writer race silently and permanently desynchronizes the index. Both should be fixed before milestone 2 builds on these interfaces.
