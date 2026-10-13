# molseal Benchmark (Milestone 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bulk ingestion to the library, then build the benchmark harness that loads the same ChEMBL slice into molseal and into the RDKit cartridge, proves identical answers, and times both, first at 500k molecules.

**Architecture:** One library change (`put_structures` in the store, `put_many` in the vault, the CLI `load` on top of it). Everything else lives under `bench/` in the same repo and never enters the package: a resumable ChEMBL loader, a cartridge container with its schema, a seeded query-set generator, a parity checker that gates timings, a timing runner, a bench container so the head-to-head shares one VM, and a one-command driver.

**Tech Stack:** As milestone 1 (Python 3.13, uv, `rdkit==2025.3.3`, SQLAlchemy 2, psycopg 3, numpy 2). Cartridge image `informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3`. ChEMBL 37 `chembl_37_chemreps.txt.gz` from the EBI FTP. Docker Desktop with the VM raised to 16 GB or more.

**Spec:** `docs/superpowers/specs/2026-10-13-molseal-poc-design.md`, sections 7 (bulk ingestion), 8 (benchmark), 9 (absence check), 12 (write-up framing). Milestone 1's review record is in `docs/superpowers/reviews/`; its deferred minors are listed in the ledger file there.

## Global Constraints

- Everything from the milestone 1 plan's Global Constraints still binds: generic-library rule, fixed match flags (chirality off, Morgan radius 2 at 2048 bits), padding and AAD formats, commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` with explicit pathspecs, no other trailer or URL.
- The library stays synchronous. `put_many` is the only new public API. Nothing under `src/molseal` imports from `bench/`.
- Bench data lives under `bench/data/` and results under `bench/results/`, both git-ignored already. ChEMBL is never committed or redistributed; the README attributes it (CC BY-SA 3.0).
- Ports: dev molseal Postgres stays on 5439. The bench compose adds the cartridge database on 5440. Both must never run at the same time during timing runs (compose profiles enforce it).
- RDKit release pinned identically on both sides: PyPI `rdkit==2025.3.3`, image tag `Release_2025_03_3`; written as "2025.03.3" in results.
- Cartridge settings, applied via the container command: `shared_buffers=4GB`, `maintenance_work_mem=2GB`, `effective_cache_size=8GB`, `work_mem=256MB`, `rdkit.morgan_fp_size=2048`, `rdkit.do_chiral_sss=off`.
- No timing is reported from a run whose parity gates failed. Results files carry N, the git SHA, the RDKit release, the image architecture and the VM allocation.
- Tests never download anything. Postgres tests skip without `MOLSEAL_TEST_DATABASE_URL`; cartridge tests skip without `MOLSEAL_CARTRIDGE_URL`.

## Review Focus

1. **A batch with the same id twice** must be rejected before any write, not silently last-wins. (Task 1)
2. **A re-sent chunk after a crash between commit and progress write** must be recognized as already loaded (deterministic ids, conflict on every row) and not duplicated. (Task 3)
3. **A SMILES the cartridge refuses but RDKit's Python layer accepts, or the reverse,** must drop out of the parity set on both sides rather than fail a gate for the wrong reason. (Task 6)
4. **A query with zero hits, or with more than 10k hits,** must pass the gates on count alone without an id-set comparison blowing up or being skipped silently. (Task 6)
5. **Timing a cold run must actually be cold:** restart the container and prove shared buffers are empty before measuring. (Task 7)

---

### Task 1: Bulk ingestion in the library

**Files:**
- Modify: `src/molseal/store.py` (Protocol + `SqlStore`), `src/molseal/vault.py`, `src/molseal/cli.py` (`cmd_load`)
- Test: `tests/test_store.py`, `tests/test_store_pg.py`, `tests/test_vault.py` (append)

**Interfaces:**
- Consumes: the merged milestone 1 code. Read `store.put_structure`, `vault._write`, `vault._advance` and `cli.cmd_load` before changing anything; this task extends them and must keep every existing test green.
- Produces:
  - `SqlStore.get_structures(tenant_id, ids: list[UUID]) -> dict[UUID, StructureRecord]`
  - `SqlStore.put_structures(batch: list[tuple[StructureRecord, dict[str, bytes], int | None]]) -> list[int]` raising `ValueError` on duplicate ids and `VersionConflict` if any row's expected version mismatches; both added to the `StructureStore` Protocol
  - `Vault.put_many(items: Iterable[tuple[UUID, str, dict[str, str] | None]], chunk: int = 10_000) -> int`
  - `cmd_load` loads through `put_many` in chunks and writes the sidecar per committed chunk

- [ ] **Step 1: Append the failing store tests**

Append to `tests/test_store.py`:
```python
from molseal.store import VersionConflict


def test_put_structures_assigns_consecutive_seqs_and_counts_live(store):
    t = uuid4()
    store.ensure_tenant(t)
    ids = [uuid4() for _ in range(5)]
    seqs = store.put_structures([(rec(t, i), {"inchikey": bytes([n]) * 32}, None) for n, i in enumerate(ids)])
    assert seqs == [1, 2, 3, 4, 5]
    assert store.tenant_state(t) == (5, 5)
    assert [r.id for r in store.changes_since(t, 0)] == ids
    assert store.find_by_key(t, "inchikey", bytes([2]) * 32) == [ids[2]]
    got = store.get_structures(t, ids[:2] + [uuid4()])
    assert set(got) == set(ids[:2]) and got[ids[0]].seq == 1


def test_put_structures_rejects_duplicate_ids_before_writing(store):
    t = uuid4()
    store.ensure_tenant(t)
    a = uuid4()
    with pytest.raises(ValueError):
        store.put_structures([(rec(t, a), {}, None), (rec(t, a), {}, None)])
    assert store.tenant_state(t) == (0, 0)


def test_put_structures_is_all_or_nothing_on_a_stale_version(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    store.put_structure(rec(t, a), {}, None)
    with pytest.raises(VersionConflict):
        store.put_structures([(rec(t, b), {}, None), (rec(t, a, rv=2), {}, 5)])
    assert store.tenant_state(t) == (1, 1)
    assert store.get_structure(t, b) is None


def test_put_structures_updates_existing_rows_and_adjusts_live_count(store):
    t = uuid4()
    store.ensure_tenant(t)
    a, b = uuid4(), uuid4()
    store.put_structures([(rec(t, a), {"k": b"a" * 32}, None), (rec(t, b), {}, None)])
    seqs = store.put_structures([(rec(t, a, rv=2, ct=b"v2"), {"k": b"c" * 32}, 1),
                                 (rec(t, b, rv=2, deleted=True), {}, 1)])
    assert seqs == [3, 4]
    assert store.tenant_state(t) == (4, 1)
    assert store.get_structure(t, a).ciphertext == b"v2"
    assert store.find_by_key(t, "k", b"a" * 32) == [] and store.find_by_key(t, "k", b"c" * 32) == [a]


def test_put_structures_empty_batch_is_a_no_op(store):
    t = uuid4()
    store.ensure_tenant(t)
    assert store.put_structures([]) == []
    assert store.tenant_state(t) == (0, 0)
```

Append to `tests/test_store_pg.py`:
```python
def test_a_batch_holds_the_cursor_lock_so_a_single_writer_lands_after_it(pg_store, pg_engine):
    t = uuid4()
    pg_store.ensure_tenant(t)
    ids = [uuid4() for _ in range(3)]
    other = uuid4()
    got = {}

    conn_a = pg_engine.connect()
    tx_a = conn_a.begin()
    end = conn_a.execute(update(tenants).where(tenants.c.tenant_id == t)
                         .values(seq_cursor=tenants.c.seq_cursor + 3)
                         .returning(tenants.c.seq_cursor)).scalar_one()
    assert end == 3

    th = threading.Thread(target=lambda: got.setdefault("seq", pg_store.put_structure(_rec(t, other), {}, None)))
    th.start()
    time.sleep(0.5)
    assert th.is_alive()
    tx_a.rollback()
    conn_a.close()
    th.join(timeout=5)
    assert got["seq"] == 1, "the rolled-back batch consumed nothing; the single writer got the next number"
    seqs = pg_store.put_structures([(_rec(t, i), {}, None) for i in ids])
    assert seqs == [2, 3, 4]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_store.py -q -k put_structures`
Expected: FAIL with `AttributeError: 'SqlStore' object has no attribute 'put_structures'`.

- [ ] **Step 3: Add the store methods**

In `src/molseal/store.py`, add to the Protocol after `put_structure`:
```python
    def get_structures(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, StructureRecord]: ...
    def put_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes], int | None]]) -> list[int]: ...
```

Add to `SqlStore` after `put_structure`:
```python
    def get_structures(self, tenant_id: UUID, ids: list[UUID]) -> dict[UUID, StructureRecord]:
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(structures)
                                .where(structures.c.tenant_id == tenant_id, structures.c.id.in_(ids))).all()
        return {r.id: _record(r) for r in rows}

    def put_structures(self, batch: list[tuple[StructureRecord, dict[str, bytes], int | None]]) -> list[int]:
        """One transaction for a whole batch: the cursor advances by the batch size (holding the tenant
        lock to commit, so the numbers stay consecutive and commit-ordered), every row is compare-and-set
        against its expected version, new rows are bulk-inserted, existing rows updated, the live count
        adjusted once, and key rows replaced. Any conflict rolls the whole batch back."""
        if not batch:
            return []
        ids = [rec.id for rec, _, _ in batch]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in batch")
        tenant_id = batch[0][0].tenant_id
        n = len(batch)
        with self.engine.begin() as conn:
            end = conn.execute(
                update(tenants).where(tenants.c.tenant_id == tenant_id)
                .values(seq_cursor=tenants.c.seq_cursor + n).returning(tenants.c.seq_cursor)
            ).scalar_one()
            first = int(end) - n + 1
            cur = {r.id: (r.row_version, bool(r.deleted)) for r in conn.execute(
                select(structures.c.id, structures.c.row_version, structures.c.deleted)
                .where(structures.c.tenant_id == tenant_id, structures.c.id.in_(ids)))}
            for rec, _, expected in batch:
                found = cur[rec.id][0] if rec.id in cur else None
                if found != expected:
                    raise VersionConflict(f"{rec.id}: expected row_version {expected}, found {found}")
            delta = sum(int(not rec.deleted) - int(rec.id in cur and not cur[rec.id][1]) for rec, _, _ in batch)
            if delta:
                conn.execute(update(tenants).where(tenants.c.tenant_id == tenant_id)
                             .values(live_count=tenants.c.live_count + delta))
            now = datetime.now(timezone.utc)
            inserts, seqs = [], []
            for offset, (rec, _, _) in enumerate(batch):
                seq = first + offset
                seqs.append(seq)
                values = dict(row_version=rec.row_version, data_key_version=rec.data_key_version,
                              nonce=rec.nonce, ciphertext=rec.ciphertext, deleted=rec.deleted, seq=seq,
                              updated_at=now)
                if rec.id in cur:
                    conn.execute(update(structures)
                                 .where(structures.c.tenant_id == tenant_id, structures.c.id == rec.id)
                                 .values(**values))
                else:
                    inserts.append(dict(tenant_id=tenant_id, id=rec.id, **values))
            if inserts:
                conn.execute(insert(structures), inserts)
            conn.execute(delete(structure_keys)
                         .where(structure_keys.c.tenant_id == tenant_id, structure_keys.c.id.in_(ids)))
            key_rows = [dict(tenant_id=tenant_id, id=rec.id, kind=kind, hmac=h)
                        for rec, hmacs, _ in batch for kind, h in hmacs.items()]
            if key_rows:
                conn.execute(insert(structure_keys), key_rows)
            return seqs
```

Run: `uv run pytest tests/test_store.py -q` then, with the Postgres URL set, `uv run pytest tests/test_store_pg.py -q`. Expected: all pass.

- [ ] **Step 4: Append the failing vault tests**

Append to `tests/test_vault.py`:
```python
def test_put_many_loads_in_chunks_with_consecutive_seqs(vault):
    from tests.mols import molecules
    smiles = molecules()[:25]
    ids = [uuid4() for _ in smiles]
    n = vault.put_many([(i, s, {"inchikey": f"K{k}"}) for k, (i, s) in enumerate(zip(ids, smiles))], chunk=10)
    assert n == 25 and len(vault) == 25
    assert vault.store.tenant_state(vault.tenant_id) == (25, 25)
    assert vault.find_exact("inchikey", "K7") == [ids[7]]
    assert set(vault.substructure("[#6]", kind="smarts")) == set(ids)
    assert vault.index.last_seq == 25


def test_put_many_edits_existing_ids_and_rejects_duplicates_and_garbage(vault):
    a, b = uuid4(), uuid4()
    vault.put_many([(a, "CCO", None), (b, "CCN", None)])
    vault.put_many([(a, "c1ccccc1", None)])
    assert vault.substructure("CCO") == [] and vault.substructure("c1ccccc1") == [a]
    assert vault.get(a).smiles == "c1ccccc1"
    with pytest.raises(ValueError):
        vault.put_many([(b, "CCO", None), (b, "CCN", None)])
    cursor_before = vault.store.tenant_state(vault.tenant_id)
    with pytest.raises(engine.StructureError):
        vault.put_many([(uuid4(), "CCO", None), (uuid4(), "garbage(", None)])
    assert vault.store.tenant_state(vault.tenant_id) == cursor_before


def test_a_second_instance_sees_a_bulk_load(env):
    store, kp, snap = env
    t = uuid4()
    v1, v2 = Vault(store, kp, t), Vault(store, kp, t)
    ids = [uuid4() for _ in range(12)]
    v1.put_many([(i, "CCO", None) for i in ids], chunk=5)
    assert set(v2.substructure("CCO")) == set(ids)
```

Run: `uv run pytest tests/test_vault.py -q -k put_many`
Expected: FAIL with `AttributeError`.

- [ ] **Step 5: Add `put_many` to the vault and generalize `_advance`**

In `src/molseal/vault.py`, add `from itertools import batched` and `from collections.abc import Iterable` to the imports, replace `_advance` with:
```python
    def _advance(self, first_seq: int, last_seq: int | None = None) -> None:
        """After our own write(s): if nothing else landed in between, remember the last seq; otherwise catch up."""
        last_seq = first_seq if last_seq is None else last_seq
        if first_seq == self.index.last_seq + 1:
            self.index.last_seq = last_seq
        else:
            self._refresh_locked(*self.store.tenant_state(self.tenant_id))
```
and add after `delete`:
```python
    def put_many(self, items: Iterable[tuple[UUID, str, dict[str, str] | None]], chunk: int = 10_000) -> int:
        """Bulk ingest. Each chunk is canonicalized and encrypted in Python, written in one store
        transaction (consecutive, commit-ordered sequence numbers), and appended to the index once.
        Raises before any write if a chunk holds a duplicate id or an unparseable SMILES."""
        total = 0
        for group in batched(items, chunk):
            total += self._put_chunk(list(group))
        return total

    def _put_chunk(self, group: list[tuple[UUID, str, dict[str, str] | None]]) -> int:
        ids = [i for i, _, _ in group]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate ids in chunk")
        prepared = [(i, canonicalize(s), dict(k or {})) for i, s, k in group]   # StructureError before any write
        with self._lock:
            for _ in range(WRITE_ATTEMPTS):
                existing = self.store.get_structures(self.tenant_id, ids)
                batch, items = [], []
                for i, canonical, ek in prepared:
                    ex = existing.get(i)
                    expected = None if ex is None else ex.row_version
                    rv = 1 if expected is None else expected + 1
                    nonce, ct = self._seal(i, rv, Payload(canonical, ek, False))
                    rec = StructureRecord(self.tenant_id, i, rv, self.keys.data_key_version, nonce, ct, False)
                    batch.append((rec, {k: exact_key_hmac(self.keys.hmac_key, k, v) for k, v in ek.items()}, expected))
                    items.append((i, canonical, rv))
                try:
                    seqs = self.store.put_structures(batch)
                    break
                except VersionConflict:
                    continue
            else:
                raise VaultError(f"chunk of {len(group)}: row_version conflict persisted after {WRITE_ATTEMPTS} attempts")
            self.index.add_batch(items, self.num_threads)
            self._advance(seqs[0], seqs[-1])
        return len(batch)
```

Run: `uv run pytest tests/test_vault.py -q`. Expected: all pass.

- [ ] **Step 6: Make the CLI `load` use `put_many`**

Replace `cmd_load` in `src/molseal/cli.py` with:
```python
LOAD_CHUNK = 10_000


def _parse_smiles_file(src: Path):
    """Yield (id, canonical_smiles, inchikey) for each parseable line; count the rest."""
    skipped = 0
    with open(src) as fin:
        for line in fin:
            tok = line.split()
            if not tok or tok[0].startswith("#"):
                continue
            mol = Chem.MolFromSmiles(tok[0])
            if mol is None:
                skipped += 1
                continue
            yield uuid.uuid4(), Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)
    _parse_smiles_file.skipped = skipped


def cmd_load(args: argparse.Namespace) -> int:
    vault = _vault(args)
    src = Path(args.file)
    sidecar = src.with_name(src.name + ".ids.tsv")
    loaded = 0
    rows = _parse_smiles_file(src)
    with open(sidecar, "a") as fout:
        while True:
            chunk = list(itertools.islice(rows, LOAD_CHUNK))
            if not chunk:
                break
            vault.put_many([(id_, smi, {"inchikey": key}) for id_, smi, key in chunk])
            for id_, smi, key in chunk:          # sidecar written only after the chunk committed
                fout.write(f"{id_}\t{smi}\t{key}\n")
            loaded += len(chunk)
            print(f"{loaded} loaded", file=sys.stderr)
    if args.snapshot_dir:
        vault.save_snapshot()
    print(f"loaded {loaded}, skipped {_parse_smiles_file.skipped}, sidecar {sidecar}")
    return 0
```
Add `import itertools` to the imports. The function attribute for `skipped` is a deliberate small trick to keep the generator simple; leave a one-line comment saying so.

Run: `uv run pytest tests/test_cli.py -q` (the existing load test must still print `loaded 30` and `skipped 1`), then the full suite both ways.

- [ ] **Step 7: Commit**

```bash
git add -- src/molseal/store.py src/molseal/vault.py src/molseal/cli.py tests/test_store.py tests/test_store_pg.py tests/test_vault.py
git commit -m "feat: bulk ingestion with put_structures and Vault.put_many; CLI load uses it

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- src/molseal/store.py src/molseal/vault.py src/molseal/cli.py tests/test_store.py tests/test_store_pg.py tests/test_vault.py
```

---

### Task 2: ChEMBL fetch and slice

**Files:**
- Create: `bench/__init__.py` (empty), `bench/fetch_chembl.py`
- Test: `tests/bench/__init__.py` (empty), `tests/bench/test_fetch.py`

**Interfaces:**
- Produces:
  - `CHEMBL_URL = "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/chembl_37_chemreps.txt.gz"`
  - `download(url: str, dest: Path) -> Path` (streams, skips if present)
  - `iter_chemreps(path: Path) -> Iterator[tuple[str, str, str]]` yielding `(chembl_id, smiles, inchikey)` from the gzipped TSV
  - `canonicalize_rows(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str] | None]` (one process)
  - `build_slice(src: Path, n: int, out: Path, workers: int) -> dict` writing `out` as TSV `chembl_id\tcanonical_smiles\tinchikey` and returning `{"requested", "loaded", "skipped"}`
  - CLI: `uv run python -m bench.fetch_chembl --n 500000 [--data bench/data] [--workers 8]` writing `bench/data/slice_500000.tsv` and `bench/data/slice_500000.meta.json`

Design choices: the first N rows in ChEMBL id order (deterministic, documented in the results). The exact key is ChEMBL's own `standard_inchi_key`, which is a caller-defined key exactly as the library intends and spares computing InChI for 2.4M rows. SMILES are re-canonicalized with RDKit 2025.03.3 so the cartridge and molseal parse the same text.

- [ ] **Step 1: Write the failing tests**

`tests/bench/test_fetch.py`:
```python
import gzip
import json

from bench import fetch_chembl as fc

HEADER = "chembl_id\tcanonical_smiles\tstandard_inchi\tstandard_inchi_key\n"
ROWS = [
    ("CHEMBL1", "OCC", "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"),
    ("CHEMBL2", "garbage(", "InChI=1S/x", "XXXXXXXXXXXXXX-XXXXXXXXXX-X"),
    ("CHEMBL3", "C1=CC=CC=C1", "InChI=1S/C6H6/c1-2-4-6-5-3-1/h1-6H", "UHOVQNZJYSORNB-UHFFFAOYSA-N"),
]


def write_chemreps(path):
    with gzip.open(path, "wt") as f:
        f.write(HEADER)
        for r in ROWS:
            f.write("\t".join(r) + "\n")


def test_iter_chemreps_yields_id_smiles_and_key(tmp_path):
    p = tmp_path / "chemreps.txt.gz"
    write_chemreps(p)
    assert list(fc.iter_chemreps(p)) == [(r[0], r[1], r[3]) for r in ROWS]


def test_canonicalize_rows_canonicalizes_and_marks_garbage():
    out = fc.canonicalize_rows([(r[0], r[1], r[3]) for r in ROWS])
    assert out[0] == ("CHEMBL1", "CCO", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N")
    assert out[1] is None
    assert out[2] == ("CHEMBL3", "c1ccccc1", "UHOVQNZJYSORNB-UHFFFAOYSA-N")


def test_build_slice_takes_first_n_and_writes_meta(tmp_path):
    src = tmp_path / "chemreps.txt.gz"
    write_chemreps(src)
    out = tmp_path / "slice_2.tsv"
    meta = fc.build_slice(src, 2, out, workers=1)
    assert meta == {"requested": 2, "loaded": 1, "skipped": 1}
    assert out.read_text() == "CHEMBL1\tCCO\tLFQSCWFLJHTTHZ-UHFFFAOYSA-N\n"
    assert json.loads(out.with_suffix(".meta.json").read_text())["loaded"] == 1


def test_download_skips_existing_file(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(b"already here")
    assert fc.download("http://127.0.0.1:9/never", p) == p
    assert p.read_bytes() == b"already here"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/bench -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench'`.

- [ ] **Step 3: Write the module**

`bench/__init__.py`: empty. `tests/bench/__init__.py`: empty.

`bench/fetch_chembl.py`:
```python
"""Fetch ChEMBL chemreps, take the first N rows, canonicalize with RDKit in a process pool.

Not part of the molseal package. Data goes under bench/data (git-ignored) and is never redistributed.
ChEMBL is CC BY-SA 3.0: https://www.ebi.ac.uk/chembl/
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from collections.abc import Iterator
from multiprocessing import Pool
from pathlib import Path

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

CHEMBL_URL = "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/chembl_37_chemreps.txt.gz"
POOL_CHUNK = 2000


def download(url: str, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if done % (50 << 20) < (1 << 20):
                print(f"{done >> 20} MiB", file=sys.stderr)
    tmp.replace(dest)
    return dest


def iter_chemreps(path: Path) -> Iterator[tuple[str, str, str]]:
    with gzip.open(path, "rt") as f:
        header = f.readline().rstrip("\n").split("\t")
        i_id, i_smi, i_key = header.index("chembl_id"), header.index("canonical_smiles"), header.index("standard_inchi_key")
        for line in f:
            cols = line.rstrip("\n").split("\t")
            yield cols[i_id], cols[i_smi], cols[i_key]


def canonicalize_rows(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str] | None]:
    out = []
    for chembl_id, smiles, key in rows:
        mol = Chem.MolFromSmiles(smiles)
        out.append(None if mol is None else (chembl_id, Chem.MolToSmiles(mol), key))
    return out


def _chunks(it: Iterator, size: int) -> Iterator[list]:
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf


def build_slice(src: Path, n: int, out: Path, workers: int) -> dict:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = iter_chemreps(src)
    first_n = (r for i, r in zip(range(n), rows))
    loaded = skipped = 0
    with open(out, "w") as f:
        if workers > 1:
            with Pool(workers) as pool:
                results = pool.imap(canonicalize_rows, _chunks(first_n, POOL_CHUNK))
                for batch in results:
                    for r in batch:
                        if r is None:
                            skipped += 1
                        else:
                            f.write("\t".join(r) + "\n")
                            loaded += 1
        else:
            for batch in _chunks(first_n, POOL_CHUNK):
                for r in canonicalize_rows(batch):
                    if r is None:
                        skipped += 1
                    else:
                        f.write("\t".join(r) + "\n")
                        loaded += 1
    meta = {"requested": n, "loaded": loaded, "skipped": skipped}
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="fetch ChEMBL chemreps and build a canonical slice")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--data", default="bench/data")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--url", default=CHEMBL_URL)
    a = p.parse_args(argv)
    data = Path(a.data)
    src = download(a.url, data / Path(a.url).name)
    out = data / f"slice_{a.n}.tsv"
    meta = build_slice(src, a.n, out, a.workers)
    print(json.dumps({**meta, "slice": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `uv run pytest tests/bench -q`. Expected: all pass. The download test must not touch the network: the file already exists, so `download` returns immediately.

- [ ] **Step 4: Fetch the real slices once**

Run: `uv run python -m bench.fetch_chembl --n 10000` then `uv run python -m bench.fetch_chembl --n 500000`. The first call downloads about 550 MB once; both reuse the file. Record the two `loaded`/`skipped` counts in the commit body. Do not commit anything under `bench/data/`.

- [ ] **Step 5: Commit**

```bash
git add -- bench/__init__.py bench/fetch_chembl.py tests/bench/__init__.py tests/bench/test_fetch.py
git commit -m "feat(bench): ChEMBL fetch and canonical slice builder

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/__init__.py bench/fetch_chembl.py tests/bench/__init__.py tests/bench/test_fetch.py
```

---

### Task 3: Resumable molseal loader and the 10k absence gate

**Files:**
- Create: `bench/load_molseal.py`, `bench/gate.py`
- Test: `tests/bench/test_load_molseal.py`

**Interfaces:**
- Consumes: `Vault.put_many`, `SqlStore`, `FileKeyProvider`, `molseal.cli.main` (for `absence-check` and `leak-report`), the slice TSV from Task 2.
- Produces:
  - `NAMESPACE = uuid.UUID("3b6f6c1e-5c0d-4c3a-9a7f-2d4e8f1a9c01")` and `row_id(chembl_id: str) -> UUID` (`uuid5`), so a re-run addresses the same rows
  - `load(slice_path: Path, store, key_provider, tenant_id, chunk: int = 10_000, snapshot_dir=None, progress_path=None) -> dict` with keys `rows, chunks, resumed_from, seconds, rows_per_second, max_rss_mb`; writes the sidecar `<slice>.ids.tsv` (`id\tsmiles\tinchikey`) per committed chunk and the progress file `<slice>.progress.json` (`{"done_chunks": k}`)
  - CLI: `uv run python -m bench.load_molseal --slice bench/data/slice_500000.tsv [--chunk 10000] [--snapshot-dir bench/data/snap]` with `--db/--key/--tenant` or the `MOLSEAL_*` env vars; writes `bench/results/load_molseal_<N>.json`
  - `uv run python -m bench.gate` runs the 10k absence gate end to end and writes `bench/results/gate_10k.json`; exit 1 on failure

Resume semantics: a chunk is one transaction, and the progress file is written only after it commits. If the process dies between the two, the chunk is sent again; because ids are deterministic, `put_many` finds the rows at version 1 and updates them to version 2. No duplicate rows, live count unchanged. That is the Review Focus item 2 test.

- [ ] **Step 1: Write the failing tests**

`tests/bench/test_load_molseal.py`:
```python
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from bench import load_molseal as lm
from molseal import keys
from molseal.store import SqlStore
from tests.mols import molecules


@pytest.fixture
def env(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    store = SqlStore(engine)
    store.create_tables()
    kp = tmp_path / "kek.key"
    keys.generate_key_file(kp)
    slice_path = tmp_path / "slice_25.tsv"
    rows = molecules()[:25]
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tKEY{i}\n" for i, s in enumerate(rows)))
    return store, keys.FileKeyProvider(kp), slice_path


def test_ids_are_deterministic():
    assert lm.row_id("CHEMBL25") == lm.row_id("CHEMBL25")
    assert lm.row_id("CHEMBL25") != lm.row_id("CHEMBL26")


def test_load_writes_sidecar_progress_and_stats(env):
    store, kp, slice_path = env
    t = uuid4()
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["rows"] == 25 and stats["chunks"] == 3 and stats["resumed_from"] == 0
    assert store.tenant_state(t) == (25, 25)
    sidecar = slice_path.with_name(slice_path.name + ".ids.tsv").read_text().splitlines()
    assert len(sidecar) == 25 and sidecar[0].split("\t")[0] == str(lm.row_id("CHEMBL0"))
    assert json.loads(slice_path.with_name(slice_path.name + ".progress.json").read_text()) == {"done_chunks": 3}


def test_resume_after_a_crash_between_commit_and_progress_does_not_duplicate(env):
    store, kp, slice_path = env
    t = uuid4()
    lm.load(slice_path, store, kp, t, chunk=10)
    progress = slice_path.with_name(slice_path.name + ".progress.json")
    progress.write_text(json.dumps({"done_chunks": 1}))          # pretend chunks 2 and 3 were never recorded
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["resumed_from"] == 1 and stats["rows"] == 15
    assert store.tenant_state(t)[1] == 25                          # live count unchanged
    assert store.get_structure(t, lm.row_id("CHEMBL24")).row_version == 2
    assert store.get_structure(t, lm.row_id("CHEMBL0")).row_version == 1
    assert json.loads(progress.read_text()) == {"done_chunks": 3}


def test_fully_loaded_slice_is_a_no_op(env):
    store, kp, slice_path = env
    t = uuid4()
    lm.load(slice_path, store, kp, t, chunk=10)
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["rows"] == 0 and stats["resumed_from"] == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/bench/test_load_molseal.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the loader**

`bench/load_molseal.py`:
```python
"""Load a canonical slice into molseal through put_many, resumably.

Ids are uuid5(NAMESPACE, chembl_id), so a re-sent chunk updates rows instead of duplicating them.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import resource
import sys
import time
import uuid
from pathlib import Path

import sqlalchemy as sa

from molseal.keys import FileKeyProvider, KeyProvider
from molseal.store import SqlStore, StructureStore
from molseal.vault import Vault

NAMESPACE = uuid.UUID("3b6f6c1e-5c0d-4c3a-9a7f-2d4e8f1a9c01")


def row_id(chembl_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, chembl_id)


def _rows(slice_path: Path):
    with open(slice_path) as f:
        for line in f:
            chembl_id, smiles, key = line.rstrip("\n").split("\t")
            yield chembl_id, smiles, key


def load(slice_path: Path, store: StructureStore, key_provider: KeyProvider, tenant_id: uuid.UUID,
         chunk: int = 10_000, snapshot_dir: Path | None = None, progress_path: Path | None = None) -> dict:
    slice_path = Path(slice_path)
    progress_path = progress_path or slice_path.with_name(slice_path.name + ".progress.json")
    sidecar = slice_path.with_name(slice_path.name + ".ids.tsv")
    done = json.loads(progress_path.read_text())["done_chunks"] if progress_path.exists() else 0
    vault = Vault(store, key_provider, tenant_id, snapshot_dir=snapshot_dir)
    rows = _rows(slice_path)
    for _ in range(done):                       # skip chunks already recorded as committed
        if not list(itertools.islice(rows, chunk)):
            break
    t0 = time.perf_counter()
    loaded = chunks = 0
    with open(sidecar, "a") as side:
        while True:
            group = list(itertools.islice(rows, chunk))
            if not group:
                break
            vault.put_many([(row_id(c), s, {"inchikey": k}) for c, s, k in group], chunk=len(group))
            for c, s, k in group:
                side.write(f"{row_id(c)}\t{s}\t{k}\n")
            chunks += 1
            loaded += len(group)
            progress_path.write_text(json.dumps({"done_chunks": done + chunks}))
            print(f"chunk {done + chunks}: {loaded} rows this run", file=sys.stderr)
    seconds = time.perf_counter() - t0
    if snapshot_dir is not None:
        vault.save_snapshot()
    return {"rows": loaded, "chunks": chunks, "resumed_from": done, "seconds": round(seconds, 2),
            "rows_per_second": round(loaded / seconds, 1) if seconds > 0 and loaded else 0.0,
            "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 << 20), 1)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="load a slice into molseal")
    p.add_argument("--slice", required=True)
    p.add_argument("--chunk", type=int, default=10_000)
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"))
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""))
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY_FILE", ""))
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""))
    p.add_argument("--results", default="bench/results")
    a = p.parse_args(argv)
    if not (a.db and a.key and a.tenant):
        raise SystemExit("--db, --key and --tenant (or MOLSEAL_* env vars) are required")
    store = SqlStore(sa.create_engine(a.db))
    store.create_tables()
    stats = load(Path(a.slice), store, FileKeyProvider(Path(a.key)), uuid.UUID(a.tenant), a.chunk,
                 Path(a.snapshot_dir) if a.snapshot_dir else None)
    n = Path(a.slice).stem.split("_")[-1]
    out = Path(a.results) / f"load_molseal_{n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**stats, "slice": a.slice, "tenant": a.tenant}, indent=1))
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note on macOS `ru_maxrss` is in bytes; on Linux it is kilobytes. The bench container (Task 8) runs Linux, so `main` detects `sys.platform` and divides by 1024 instead of `1 << 20` there; add that two-line branch.

Run: `uv run pytest tests/bench/test_load_molseal.py -q`. Expected: all pass.

- [ ] **Step 4: Write the 10k gate driver**

`bench/gate.py`:
```python
"""Milestone 2's first gate: load a 10k slice, then prove absence with a real dump, then the leak report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa

from bench.load_molseal import load
from molseal import cli
from molseal.keys import FileKeyProvider, generate_key_file
from molseal.store import SqlStore

DB = os.environ.get("MOLSEAL_DATABASE_URL", "postgresql+psycopg://molseal:molseal@localhost:5439/molseal")
RESULTS = Path("bench/results")


def run_dump(dest: Path) -> None:
    with open(dest, "wb") as f:
        subprocess.run(["docker", "compose", "exec", "-T", "db", "pg_dump", "-Fp", "--compress=0",
                        "-U", "molseal", "molseal"], check=True, stdout=f)


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    slice_path = Path("bench/data/slice_10000.tsv")
    if not slice_path.exists():
        raise SystemExit("run: uv run python -m bench.fetch_chembl --n 10000")
    key = RESULTS / "gate.key"
    if not key.exists():
        generate_key_file(key)
    tenant = uuid.uuid4()
    store = SqlStore(sa.create_engine(DB))
    store.create_tables()
    stats = load(slice_path, store, FileKeyProvider(key), tenant, progress_path=RESULTS / "gate.progress.json")
    sidecar = str(slice_path.with_name(slice_path.name + ".ids.tsv"))
    common = ["--db", DB, "--key", str(key), "--tenant", str(tenant)]
    rc1 = cli.main(["absence-check", sidecar, *common])          # plants the control row
    run_dump(RESULTS / "dump_10k.sql")
    rc2 = cli.main(["absence-check", sidecar, "--dump", str(RESULTS / "dump_10k.sql"), *common])
    rc3 = cli.main(["leak-report", sidecar, *common])
    ok = rc1 == 0 and rc2 == 0 and rc3 == 0
    (RESULTS / "gate_10k.json").write_text(json.dumps({"load": stats, "absence_db": rc1, "absence_dump": rc2,
                                                      "leak_report": rc3, "ok": ok}, indent=1))
    print("GATE OK" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

`absence-check` and `leak-report` print their JSON to stdout; run the gate with `| tee bench/results/gate_10k.log` so both JSON blocks are kept.

- [ ] **Step 5: Run the gate**

With the dev database up (`docker compose up -d db`): `uv run python -m bench.gate | tee bench/results/gate_10k.log`. Expected: `GATE OK`, absence JSON with `ok: true`, control found in DB and dump legs, 0 hits, needles about 20k minus short ones. Paste the absence JSON and the leak-report JSON into the commit body. If the gate fails, stop and report; do not commit a failing gate.

- [ ] **Step 6: Commit**

```bash
git add -- bench/load_molseal.py bench/gate.py tests/bench/test_load_molseal.py
git commit -m "feat(bench): resumable molseal loader and the 10k absence gate

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/load_molseal.py bench/gate.py tests/bench/test_load_molseal.py
```

---

### Task 4: Cartridge container and loader

**Files:**
- Create: `bench/compose.yml`, `bench/load_cartridge.py`
- Test: `tests/bench/test_load_cartridge.py` (skips without `MOLSEAL_CARTRIDGE_URL`)

**Interfaces:**
- Consumes: `bench.load_molseal.row_id` so both databases hold the same ids for the same ChEMBL rows.
- Produces:
  - compose service `cartridge-db` (profile `cartridge`) on `127.0.0.1:5440`, image `informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3`, settings from Global Constraints via the container command, user/password/db `bench`
  - `CARTRIDGE_URL = "postgresql+psycopg://bench:bench@localhost:5440/bench"`
  - `ensure_schema(engine)`: `CREATE EXTENSION IF NOT EXISTS rdkit`, `pg_prewarm`, table `mols(id uuid PK, chembl_id text, smiles text, m mol, mfp bfp)`
  - `load(slice_path, engine, batch=5000) -> dict` idempotent (`ON CONFLICT (id) DO NOTHING`), then `UPDATE mols SET mfp = morganbv_fp(m) WHERE mfp IS NULL AND m IS NOT NULL`, then the two GiST indexes and `ANALYZE`; returns `{rows, rejected, insert_seconds, fp_seconds, gist_m_seconds, gist_mfp_seconds, morgan_fp_size, do_chiral_sss}` and writes `bench/results/cartridge_rejected_<N>.txt` (one id per line for rows where `m IS NULL`)
  - CLI: `uv run python -m bench.load_cartridge --slice bench/data/slice_500000.tsv [--url ...]` writing `bench/results/load_cartridge_<N>.json`

- [ ] **Step 1: Write the compose file**

`bench/compose.yml`:
```yaml
# Benchmark databases. Only one profile runs at a time during timing runs.
#   docker compose -f bench/compose.yml --profile cartridge up -d
#   docker compose -f bench/compose.yml --profile cartridge down
services:
  cartridge-db:
    image: informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3
    profiles: ["cartridge"]
    environment:
      POSTGRES_USER: bench
      POSTGRES_PASSWORD: bench
      POSTGRES_DB: bench
    command: >
      postgres -c shared_buffers=4GB -c maintenance_work_mem=2GB -c effective_cache_size=8GB
               -c work_mem=256MB -c rdkit.morgan_fp_size=2048 -c rdkit.do_chiral_sss=off
    shm_size: 1g
    ports:
      - "127.0.0.1:5440:5432"
    volumes:
      - cartridge-pgdata:/var/lib/postgresql/data
volumes:
  cartridge-pgdata:
```

Start it: `docker compose -f bench/compose.yml --profile cartridge up -d`, wait for `docker compose -f bench/compose.yml exec -T cartridge-db pg_isready -U bench`, then check the settings took: `docker compose -f bench/compose.yml exec -T cartridge-db psql -U bench -c "SHOW shared_buffers" -c "SHOW rdkit.morgan_fp_size"`. If `rdkit.morgan_fp_size` is reported as unrecognized before the extension is created, create the extension once (`CREATE EXTENSION rdkit`) and re-check; the placeholder becomes a real setting after that. Print `docker image inspect --format '{{.Architecture}}' informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3` and keep the output for the results file (expected `arm64`).

- [ ] **Step 2: Write the failing integration test**

`tests/bench/test_load_cartridge.py`:
```python
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from bench import load_cartridge as lc
from bench.load_molseal import row_id
from tests.mols import molecules


@pytest.fixture(scope="module")
def cart_engine():
    url = os.environ.get("MOLSEAL_CARTRIDGE_URL")
    if not url:
        pytest.skip("MOLSEAL_CARTRIDGE_URL not set")
    engine = sa.create_engine(url)
    lc.ensure_schema(engine)
    yield engine
    engine.dispose()


def test_load_parses_counts_rejected_and_pins_settings(cart_engine, tmp_path):
    slice_path = tmp_path / "slice_21.tsv"
    rows = [(f"T{i}", s, f"K{i}") for i, s in enumerate(molecules()[:20])] + [("T20", "C1CC", "K20")]
    slice_path.write_text("".join("\t".join(r) + "\n" for r in rows))
    with cart_engine.begin() as c:
        c.execute(sa.text("DELETE FROM mols WHERE chembl_id LIKE 'T%'"))
    stats = lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)
    assert stats["rows"] == 21 and stats["rejected"] == 1
    assert stats["morgan_fp_size"] == "2048" and stats["do_chiral_sss"] == "off"
    assert (tmp_path / "cartridge_rejected_21.txt").read_text().strip() == str(row_id("T20"))
    with cart_engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND mfp IS NOT NULL")).scalar_one()
        hit = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND m @> mol_from_smiles('c1ccccc1'::cstring)")).scalar_one()
    assert n == 20 and hit > 0
    assert lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)["rows"] == 21   # idempotent
```

`C1CC` is a SMILES RDKit rejects (unclosed ring); the cartridge's `mol_from_smiles` returns NULL for it, which is the rejected path.

- [ ] **Step 3: Write the loader**

`bench/load_cartridge.py`:
```python
"""Load the same slice into the RDKit cartridge, the fair baseline. Same ids as the molseal side."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

from bench.load_molseal import row_id

CARTRIDGE_URL = "postgresql+psycopg://bench:bench@localhost:5440/bench"

SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS rdkit",
    "CREATE EXTENSION IF NOT EXISTS pg_prewarm",
    "CREATE TABLE IF NOT EXISTS mols (id uuid PRIMARY KEY, chembl_id text NOT NULL, smiles text NOT NULL, m mol, mfp bfp)",
]
INSERT = text("INSERT INTO mols (id, chembl_id, smiles, m) VALUES (:id, :cid, :smi, mol_from_smiles(CAST(:smi AS cstring))) "
              "ON CONFLICT (id) DO NOTHING")


def ensure_schema(engine: sa.Engine) -> None:
    with engine.begin() as c:
        for stmt in SCHEMA:
            c.execute(text(stmt))


def _rows(slice_path: Path):
    with open(slice_path) as f:
        for line in f:
            cid, smi, _ = line.rstrip("\n").split("\t")
            yield {"id": row_id(cid), "cid": cid, "smi": smi}


def _timed(engine: sa.Engine, stmt: str) -> float:
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(text(stmt))
    return round(time.perf_counter() - t0, 2)


def load(slice_path: Path, engine: sa.Engine, batch: int = 5000, results_dir: Path = Path("bench/results")) -> dict:
    ensure_schema(engine)
    rows = _rows(slice_path)
    n = 0
    t0 = time.perf_counter()
    while True:
        group = list(itertools.islice(rows, batch))
        if not group:
            break
        with engine.begin() as c:
            c.execute(INSERT, group)
        n += len(group)
        print(f"{n} rows", file=sys.stderr)
    insert_seconds = round(time.perf_counter() - t0, 2)
    fp_seconds = _timed(engine, "UPDATE mols SET mfp = morganbv_fp(m) WHERE mfp IS NULL AND m IS NOT NULL")
    gist_m = _timed(engine, "CREATE INDEX IF NOT EXISTS mols_m_gist ON mols USING gist (m)")
    gist_mfp = _timed(engine, "CREATE INDEX IF NOT EXISTS mols_mfp_gist ON mols USING gist (mfp)")
    _timed(engine, "ANALYZE mols")
    with engine.connect() as c:
        rejected = [r.id for r in c.execute(text("SELECT id FROM mols WHERE m IS NULL"))]
        fp_size = c.execute(text("SHOW rdkit.morgan_fp_size")).scalar_one()
        chiral = c.execute(text("SHOW rdkit.do_chiral_sss")).scalar_one()
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    n_label = Path(slice_path).stem.split("_")[-1]
    (results_dir / f"cartridge_rejected_{n_label}.txt").write_text("".join(f"{i}\n" for i in rejected))
    return {"rows": n, "rejected": len(rejected), "insert_seconds": insert_seconds, "fp_seconds": fp_seconds,
            "gist_m_seconds": gist_m, "gist_mfp_seconds": gist_mfp,
            "morgan_fp_size": str(fp_size), "do_chiral_sss": str(chiral)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="load a slice into the RDKit cartridge")
    p.add_argument("--slice", required=True)
    p.add_argument("--url", default=CARTRIDGE_URL)
    p.add_argument("--batch", type=int, default=5000)
    p.add_argument("--results", default="bench/results")
    a = p.parse_args(argv)
    stats = load(Path(a.slice), sa.create_engine(a.url), a.batch, Path(a.results))
    n = Path(a.slice).stem.split("_")[-1]
    Path(a.results, f"load_cartridge_{n}.json").write_text(json.dumps({**stats, "slice": a.slice}, indent=1))
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run with `export MOLSEAL_CARTRIDGE_URL=postgresql+psycopg://bench:bench@localhost:5440/bench`: `uv run pytest tests/bench/test_load_cartridge.py -q`. Expected: 1 passed; without the variable, 1 skipped. If `SHOW rdkit.morgan_fp_size` returns 512, the `-c` placeholder did not take: add `ALTER DATABASE bench SET rdkit.morgan_fp_size = 2048` and `ALTER DATABASE bench SET rdkit.do_chiral_sss = off` to `SCHEMA` after the extension line, reconnect, and note it in the commit body.

- [ ] **Step 4: Commit**

```bash
git add -- bench/compose.yml bench/load_cartridge.py tests/bench/test_load_cartridge.py
git commit -m "feat(bench): cartridge container with pinned settings and idempotent loader

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/compose.yml bench/load_cartridge.py tests/bench/test_load_cartridge.py
```

---

### Task 5: Seeded query set

**Files:**
- Create: `bench/queries.py`
- Test: `tests/bench/test_queries.py`

**Interfaces:**
- Produces:
  - `FUNCTIONAL_GROUPS: list[tuple[str, str]]` of `(name, smarts)`, about 20 entries including ring queries
  - `fragments_from(smiles_list, rng, n=50, min_atoms=5, max_atoms=20) -> list[str]` BRICS fragments with dummy atoms removed, re-canonicalized, deduplicated, spread across sizes
  - `build_queries(slice_path, seed=20261013, n_frag=50, n_sim=50, sample=2000) -> dict` with `substructure: [{"id", "kind", "query"}]` and `similarity: [{"id", "query", "source_id"}]`
  - CLI: `uv run python -m bench.queries --slice bench/data/slice_500000.tsv` writing `bench/data/queries_<N>.json`

- [ ] **Step 1: Write the failing tests**

`tests/bench/test_queries.py`:
```python
import json
import random

from rdkit import Chem

from bench import queries as q
from tests.mols import molecules


def test_functional_groups_all_parse_and_include_ring_queries():
    names = {n for n, _ in q.FUNCTIONAL_GROUPS}
    assert len(q.FUNCTIONAL_GROUPS) >= 20
    assert {"ring-atom", "five-ring", "ring-junction"} <= names
    for _, smarts in q.FUNCTIONAL_GROUPS:
        assert Chem.MolFromSmarts(smarts) is not None


def test_fragments_are_sized_deduplicated_and_free_of_dummies():
    frags = q.fragments_from(molecules(), random.Random(1), n=20)
    assert 1 <= len(frags) <= 20 and len(set(frags)) == len(frags)
    for f in frags:
        m = Chem.MolFromSmiles(f)
        assert m is not None and "*" not in f
        assert 5 <= m.GetNumHeavyAtoms() <= 20


def test_build_queries_is_deterministic(tmp_path):
    slice_path = tmp_path / "slice_438.tsv"
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tK{i}\n" for i, s in enumerate(molecules())))
    a = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    b = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    assert a == b
    assert len(a["similarity"]) == 5 and all(s["source_id"] for s in a["similarity"])
    kinds = {s["kind"] for s in a["substructure"]}
    assert kinds == {"smiles", "smarts"}
    assert json.dumps(a)   # serializable
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/bench/test_queries.py -q`. Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

`bench/queries.py`:
```python
"""Seeded query set: BRICS fragments of real molecules at varied sizes, functional-group and ring
SMARTS, and similarity queries drawn from the slice. Published with the results."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import BRICS

from bench.load_molseal import row_id

RDLogger.DisableLog("rdApp.*")

FUNCTIONAL_GROUPS: list[tuple[str, str]] = [
    ("amide", "C(=O)N"), ("carboxylic-acid", "C(=O)[OH]"), ("ester", "C(=O)O[#6]"), ("sulfonamide", "S(=O)(=O)N"),
    ("nitrile", "C#N"), ("nitro", "[N+](=O)[O-]"), ("halogen", "[Cl,Br,I]"), ("trifluoromethyl", "C(F)(F)F"),
    ("phenol", "c[OH]"), ("aniline", "c[NH2]"), ("pyridine", "n1ccccc1"), ("piperidine", "C1CCNCC1"),
    ("piperazine", "N1CCNCC1"), ("morpholine", "C1COCCN1"), ("indole", "c1ccc2[nH]ccc2c1"), ("urea", "NC(=O)N"),
    ("ring-atom", "[R]"), ("five-ring", "[r5]"), ("ring-junction", "[x3]"), ("ring-bond-CN", "[#6]@[#7]"),
    ("non-amide-N", "[#7;!$(N-C=O)]"), ("alcohol", "[CX4][OX2H]"),
]
_DUMMY = Chem.MolFromSmarts("[#0]")


def fragments_from(smiles_list: list[str], rng: random.Random, n: int = 50, min_atoms: int = 5, max_atoms: int = 20) -> list[str]:
    seen: dict[str, int] = {}
    for smi in smiles_list:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        for frag in BRICS.BRICSDecompose(mol):
            fm = Chem.MolFromSmiles(frag)
            if fm is None:
                continue
            fm = Chem.DeleteSubstructs(fm, _DUMMY)
            try:
                Chem.SanitizeMol(fm)
            except Exception:
                continue
            if fm.GetNumAtoms() == 0 or len(Chem.GetMolFrags(fm)) != 1:
                continue
            canon = Chem.MolToSmiles(fm)
            atoms = fm.GetNumHeavyAtoms()
            if min_atoms <= atoms <= max_atoms:
                seen[canon] = atoms
    pool = sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))
    if len(pool) <= n:
        return [s for s, _ in pool]
    step = len(pool) / n                       # evenly spaced across sizes, seeded jitter inside each step
    picks = [pool[min(len(pool) - 1, int(i * step + rng.random() * step))][0] for i in range(n)]
    return list(dict.fromkeys(picks))


def build_queries(slice_path: Path, seed: int = 20261013, n_frag: int = 50, n_sim: int = 50, sample: int = 2000) -> dict:
    rng = random.Random(seed)
    rows = [line.rstrip("\n").split("\t") for line in open(slice_path)]
    picked = rng.sample(rows, min(sample, len(rows)))
    frags = fragments_from([r[1] for r in picked], rng, n=n_frag)
    sub = [{"id": f"frag-{i:03d}", "kind": "smiles", "query": f} for i, f in enumerate(frags)]
    sub += [{"id": f"fg-{name}", "kind": "smarts", "query": smarts} for name, smarts in FUNCTIONAL_GROUPS]
    sims = rng.sample(rows, min(n_sim, len(rows)))
    sim = [{"id": f"sim-{i:03d}", "query": r[1], "source_id": str(row_id(r[0]))} for i, r in enumerate(sims)]
    return {"seed": seed, "slice": str(slice_path), "substructure": sub, "similarity": sim}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="build the seeded query set for a slice")
    p.add_argument("--slice", required=True)
    p.add_argument("--seed", type=int, default=20261013)
    a = p.parse_args(argv)
    qs = build_queries(Path(a.slice), a.seed)
    n = Path(a.slice).stem.split("_")[-1]
    out = Path(a.slice).parent / f"queries_{n}.json"
    out.write_text(json.dumps(qs, indent=1))
    print(f"{len(qs['substructure'])} substructure, {len(qs['similarity'])} similarity -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Run: `uv run pytest tests/bench/test_queries.py -q`. Expected: all pass. Then `uv run python -m bench.queries --slice bench/data/slice_500000.tsv`.

- [ ] **Step 4: Commit**

```bash
git add -- bench/queries.py tests/bench/test_queries.py
git commit -m "feat(bench): seeded query set with BRICS fragments, functional-group and ring SMARTS

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/queries.py tests/bench/test_queries.py
```

---

### Task 6: Parity gates

**Files:**
- Create: `bench/parity.py`
- Test: `tests/bench/test_parity.py`

**Interfaces:**
- Consumes: `Vault` (molseal side), the cartridge engine, `bench/data/queries_<N>.json`, `bench/results/cartridge_rejected_<N>.txt`.
- Produces:
  - pure helpers: `compare_sets(a: set, b: set) -> dict` (`equal, only_a, only_b` counts, up to 5 example ids each) and `compare_scores(a: dict, b: dict, tol=1e-6) -> dict`
  - `cartridge_query_sql(kind) -> str` building `mol_from_smiles(CAST(:q AS cstring))` or `qmol_from_smarts(CAST(:q AS cstring))`
  - `run(vault, cart_engine, queries: dict, rejected: set[UUID], sample_ids: list[UUID], threshold=0.7, id_set_limit=10_000) -> dict` with `gate1..gate4` results and `ok`
  - CLI: `uv run python -m bench.parity --n 500000 [--cartridge-url ...]` with molseal `--db/--key/--tenant` (env fallbacks) writing `bench/results/parity_<N>.json`; exit 1 unless every gate passed

Gate rules, exactly as spec 8.4: (1) 100 sampled ids: `bfp_to_binary_text(mfp)` equals `DataStructs.BitVectToBinaryText` of the Python Morgan fingerprint of the molseal-decrypted SMILES; (2) every substructure query: molseal's uncapped id set minus the rejected ids has the same size as the cartridge count; (3) queries whose count is at most 10k: the id sets are identical; (4) similarity at 0.7: id sets identical excluding the source id, scores within 1e-6. Rejected ids (cartridge `m IS NULL`) are removed from the molseal side before every comparison, which is Review Focus item 3.

- [ ] **Step 1: Write the failing unit tests**

`tests/bench/test_parity.py`:
```python
from uuid import uuid4

from bench import parity


def test_compare_sets_reports_differences_with_examples():
    a, b, c = uuid4(), uuid4(), uuid4()
    r = parity.compare_sets({a, b}, {b, c})
    assert r["equal"] is False and r["only_a"] == 1 and r["only_b"] == 1
    assert r["examples_only_a"] == [str(a)] and r["examples_only_b"] == [str(c)]
    assert parity.compare_sets({a}, {a})["equal"] is True


def test_compare_scores_uses_tolerance():
    a, b = uuid4(), uuid4()
    assert parity.compare_scores({a: 0.5, b: 0.9}, {a: 0.5000000001, b: 0.9})["equal"] is True
    r = parity.compare_scores({a: 0.5}, {a: 0.6})
    assert r["equal"] is False and r["max_abs_diff"] > 0.09


def test_cartridge_query_sql_picks_the_constructor():
    assert "mol_from_smiles" in parity.cartridge_query_sql("smiles")
    assert "qmol_from_smarts" in parity.cartridge_query_sql("smarts")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/bench/test_parity.py -q`. Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

`bench/parity.py`:
```python
"""The four parity gates. No timing is published from a run where any gate fails."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from sqlalchemy import text

from bench.load_cartridge import CARTRIDGE_URL
from molseal.keys import FileKeyProvider
from molseal.store import SqlStore
from molseal.vault import Vault

_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)


def compare_sets(a: set, b: set) -> dict:
    only_a, only_b = a - b, b - a
    return {"equal": not only_a and not only_b, "size_a": len(a), "size_b": len(b),
            "only_a": len(only_a), "only_b": len(only_b),
            "examples_only_a": sorted(str(i) for i in list(only_a)[:5]),
            "examples_only_b": sorted(str(i) for i in list(only_b)[:5])}


def compare_scores(a: dict, b: dict, tol: float = 1e-6) -> dict:
    common = set(a) & set(b)
    diffs = [abs(a[i] - b[i]) for i in common]
    mx = max(diffs) if diffs else 0.0
    return {"equal": set(a) == set(b) and mx <= tol, "compared": len(common), "max_abs_diff": mx}


def cartridge_query_sql(kind: str) -> str:
    return "qmol_from_smarts(CAST(:q AS cstring))" if kind == "smarts" else "mol_from_smiles(CAST(:q AS cstring))"


def _cart_ids(conn, kind: str, query: str) -> set[uuid.UUID]:
    rows = conn.execute(text(f"SELECT id FROM mols WHERE m @> {cartridge_query_sql(kind)}"), {"q": query})
    return {r.id for r in rows}


def _cart_count(conn, kind: str, query: str) -> int:
    return int(conn.execute(text(f"SELECT count(*) FROM mols WHERE m @> {cartridge_query_sql(kind)}"), {"q": query}).scalar_one())


def _cart_similar(conn, query: str, threshold: float) -> dict[uuid.UUID, float]:
    conn.execute(text(f"SET rdkit.tanimoto_threshold = {threshold}"))
    rows = conn.execute(text(
        "SELECT id, tanimoto_sml(mfp, morganbv_fp(mol_from_smiles(CAST(:q AS cstring)))) AS s FROM mols "
        "WHERE mfp % morganbv_fp(mol_from_smiles(CAST(:q AS cstring)))"), {"q": query})
    return {r.id: float(r.s) for r in rows}


def run(vault: Vault, cart_engine: sa.Engine, queries: dict, rejected: set[uuid.UUID], sample_ids: list[uuid.UUID],
        threshold: float = 0.7, id_set_limit: int = 10_000) -> dict:
    out: dict = {"gate1": {}, "gate2": [], "gate3": [], "gate4": []}
    with cart_engine.connect() as conn:
        # Gate 1: fingerprint bytes
        mism = 0
        rows = conn.execute(text("SELECT id, bfp_to_binary_text(mfp) AS b FROM mols WHERE id = ANY(:ids)"), {"ids": sample_ids})
        cart_fp = {r.id: bytes(r.b) for r in rows}
        for i in sample_ids:
            smi = vault.get(i).smiles
            py = DataStructs.BitVectToBinaryText(_GEN.GetFingerprint(Chem.MolFromSmiles(smi)))
            if cart_fp.get(i) != py:
                mism += 1
        out["gate1"] = {"sampled": len(sample_ids), "mismatches": mism, "ok": mism == 0}
        # Gates 2 and 3: substructure
        for sq in queries["substructure"]:
            ms = set(vault.substructure(sq["query"], kind=sq["kind"])) - rejected
            cc = _cart_count(conn, sq["kind"], sq["query"])
            rec = {"id": sq["id"], "molseal": len(ms), "cartridge": cc, "ok": len(ms) == cc}
            out["gate2"].append(rec)
            if cc <= id_set_limit:
                cmp = compare_sets(ms, _cart_ids(conn, sq["kind"], sq["query"]))
                out["gate3"].append({"id": sq["id"], **cmp, "ok": cmp["equal"]})
        # Gate 4: similarity
        for sq in queries["similarity"]:
            src = uuid.UUID(sq["source_id"])
            ms = {i: s for i, s in vault.similar(sq["query"], threshold=threshold) if i != src and i not in rejected}
            cs = {i: s for i, s in _cart_similar(conn, sq["query"], threshold).items() if i != src}
            cmp = compare_scores(ms, cs)
            out["gate4"].append({"id": sq["id"], **cmp, "molseal": len(ms), "cartridge": len(cs), "ok": cmp["equal"]})
    out["ok"] = (out["gate1"]["ok"] and all(r["ok"] for r in out["gate2"]) and all(r["ok"] for r in out["gate3"])
                 and all(r["ok"] for r in out["gate4"]))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="run the parity gates")
    p.add_argument("--n", required=True)
    p.add_argument("--cartridge-url", default=os.environ.get("MOLSEAL_CARTRIDGE_URL", CARTRIDGE_URL))
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""))
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY_FILE", ""))
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""))
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"))
    p.add_argument("--seed", type=int, default=20261013)
    a = p.parse_args(argv)
    queries = json.loads(Path(f"bench/data/queries_{a.n}.json").read_text())
    rej_path = Path(f"bench/results/cartridge_rejected_{a.n}.txt")
    rejected = {uuid.UUID(l) for l in rej_path.read_text().split()} if rej_path.exists() else set()
    store = SqlStore(sa.create_engine(a.db))
    vault = Vault(store, FileKeyProvider(Path(a.key)), uuid.UUID(a.tenant), snapshot_dir=Path(a.snapshot_dir) if a.snapshot_dir else None)
    sidecar = Path(f"bench/data/slice_{a.n}.tsv.ids.tsv")
    ids = [uuid.UUID(l.split("\t")[0]) for l in sidecar.read_text().splitlines()]
    sample_ids = [i for i in random.Random(a.seed).sample(ids, min(100, len(ids))) if i not in rejected]
    result = run(vault, sa.create_engine(a.cartridge_url), queries, rejected, sample_ids)
    out = Path(f"bench/results/parity_{a.n}.json")
    out.write_text(json.dumps(result, indent=1))
    bad = [r["id"] for g in ("gate2", "gate3", "gate4") for r in result[g] if not r["ok"]]
    print(f"parity {'OK' if result['ok'] else 'FAILED'}: gate1 mismatches {result['gate1']['mismatches']}, failing queries {bad[:10]} -> {out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
```

If `id = ANY(:ids)` with a Python list of UUIDs is rejected by psycopg, pass `[str(i) for i in sample_ids]` and cast in SQL: `id = ANY(CAST(:ids AS uuid[]))`.

Run: `uv run pytest tests/bench/test_parity.py -q`. Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add -- bench/parity.py tests/bench/test_parity.py
git commit -m "feat(bench): parity gates against the cartridge

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/parity.py tests/bench/test_parity.py
```

---

### Task 7: Timing runner

**Files:**
- Create: `bench/run_bench.py`
- Test: `tests/bench/test_run_bench.py`

**Interfaces:**
- Consumes: `Vault`, the cartridge engine, `queries_<N>.json`, `cartridge_rejected_<N>.txt`, `parity_<N>.json` (must exist with `ok: true`, otherwise the runner refuses to start).
- Produces:
  - pure helpers: `median(xs)`, `percentile(xs, p)`, `time_calls(fn, repeats) -> float` (one warm-up call, then `repeats` timed calls, median seconds), `summarize(results: list[dict]) -> str` (Markdown table)
  - `cold_check(cart_engine) -> int` returning the number of non-empty shared buffers (`pg_buffercache`); the cold run records it and refuses to label a run cold if it is above 1% of `shared_buffers`
  - `run_molseal(vault, queries, rejected, threads, repeats, end_to_end: bool) -> list[dict]`
  - `run_cartridge(engine, queries, repeats, capped: bool, consumer_path_queries: int) -> list[dict]`
  - CLI: `uv run python -m bench.run_bench --n 500000 --side molseal|cartridge --mode cold|warm --where vm|host [--threads 1 -1] [--repeats 5] [--restart-cartridge] [--prewarm]` writing `bench/results/timings_<N>_<side>_<where>_<mode>[_t<threads>].json`; and `uv run python -m bench.run_bench --n 500000 --report` writing `bench/results/timings_<N>.md` from every JSON for that N

Each result row: `{"side", "where", "mode", "threads", "query_id", "kind", "op", "capped", "hits", "median_s", "repeats"}` where `op` is one of `substructure`, `count`, `similar`, `topk100`, `end_to_end`, `consumer_path`. Metadata written once per file: N, git SHA, RDKit version, Python version, platform, image architecture, VM cpu count and memory (`docker info`), `ru_maxrss`, snapshot size and cold-load seconds (molseal), `pg_buffercache` count (cartridge cold), container memory from `docker stats --no-stream` (cartridge).

Rules that keep the comparison honest (spec 8.5): one warm-up then at least five repeats per query, medians per query, p50 and p95 across queries in the report; molseal threads 1 and all reported as separate rows; the capped runs (`max_results=1000` vs `LIMIT 1000`) compared on count only; the unindexed consumer path runs on the first `--consumer-path-queries 5` substructure queries only, once each, warm, and the report labels it "consumer's current path"; molseal end-to-end includes the tenant-state read and `get_structures` of the hit ids.

- [ ] **Step 1: Write the failing unit tests**

`tests/bench/test_run_bench.py`:
```python
from bench import run_bench as rb


def test_percentiles_and_median():
    xs = [5, 1, 4, 2, 3]
    assert rb.median(xs) == 3
    assert rb.percentile(xs, 50) == 3
    assert rb.percentile(xs, 95) == 5
    assert rb.percentile([7], 95) == 7


def test_time_calls_warms_up_then_takes_the_median():
    calls = []
    def fn():
        calls.append(1)
    t = rb.time_calls(fn, repeats=3)
    assert len(calls) == 4 and t >= 0.0


def test_summarize_groups_rows_into_a_table_with_p50_and_p95():
    rows = [
        {"side": "molseal", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.010, "query_id": "a", "hits": 5},
        {"side": "molseal", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.030, "query_id": "b", "hits": 50},
        {"side": "cartridge", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.020, "query_id": "a", "hits": 5},
    ]
    md = rb.summarize(rows)
    assert "| molseal | vm | warm | 1 | substructure | uncapped | 2 |" in md
    assert "cartridge" in md and "p95" in md
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/bench/test_run_bench.py -q`. Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the runner**

`bench/run_bench.py`:
```python
"""Timings for both sides, with the labels that keep the comparison honest."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
import uuid
from pathlib import Path

import rdkit
import sqlalchemy as sa
from sqlalchemy import text

from bench.load_cartridge import CARTRIDGE_URL
from bench.parity import cartridge_query_sql
from molseal.keys import FileKeyProvider
from molseal.store import SqlStore
from molseal.vault import Vault


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def percentile(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[k]


def time_calls(fn, repeats: int) -> float:
    fn()                                    # warm-up
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return median(samples)


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except Exception as exc:              # metadata only; never fail a run over it
        return f"unavailable ({exc.__class__.__name__})"


def metadata(n: str, where: str) -> dict:
    return {"n": n, "where": where, "git_sha": _sh(["git", "rev-parse", "--short", "HEAD"]),
            "rdkit": rdkit.__version__, "python": platform.python_version(), "platform": platform.platform(),
            "image_arch": _sh(["docker", "image", "inspect", "--format", "{{.Architecture}}",
                               "informaticsmatters/rdkit-cartridge-debian:Release_2025_03_3"]),
            "docker_vm": _sh(["docker", "info", "--format", "{{.NCPU}} cpus, {{.MemTotal}} bytes"])}


def cold_check(engine: sa.Engine) -> int:
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_buffercache"))
        return int(c.execute(text("SELECT count(*) FROM pg_buffercache WHERE relfilenode <> 0")).scalar_one())


def run_molseal(vault: Vault, queries: dict, rejected: set[uuid.UUID], threads: int, repeats: int, end_to_end: bool) -> list[dict]:
    vault.num_threads = threads
    idx = vault.index
    rows = []
    base = {"side": "molseal", "threads": threads, "repeats": repeats}
    for sq in queries["substructure"]:
        q, k = sq["query"], sq["kind"]
        hits = len(set(idx.substructure(q, k, None, threads)) - rejected)
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": False, "hits": hits,
                     "median_s": time_calls(lambda: idx.substructure(q, k, None, threads), repeats)})
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": True, "hits": min(hits, 1000),
                     "median_s": time_calls(lambda: idx.substructure(q, k, 1000, threads), repeats)})
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "count", "capped": False, "hits": hits,
                     "median_s": time_calls(lambda: idx.count_substructure(q, k, threads), repeats)})
        if end_to_end:
            def e2e():
                ids = vault.substructure(q, kind=k)                      # includes the tenant-state read
                vault.store.get_structures(vault.tenant_id, ids[:1000])   # fetch the hit rows (capped fetch)
            rows.append({**base, "query_id": sq["id"], "kind": k, "op": "end_to_end", "capped": True, "hits": hits,
                         "median_s": time_calls(e2e, repeats)})
    for sq in queries["similarity"]:
        q = sq["query"]
        hits = len(idx.similar(q, 0.7))
        rows.append({**base, "query_id": sq["id"], "kind": "smiles", "op": "similar", "capped": False, "hits": hits,
                     "median_s": time_calls(lambda: idx.similar(q, 0.7), repeats)})
        rows.append({**base, "query_id": sq["id"], "kind": "smiles", "op": "topk100", "capped": True, "hits": 100,
                     "median_s": time_calls(lambda: idx.similar(q, 0.0, 100), repeats)})
    return rows


def run_cartridge(engine: sa.Engine, queries: dict, repeats: int, consumer_path_queries: int) -> list[dict]:
    rows = []
    base = {"side": "cartridge", "threads": 1, "repeats": repeats}
    with engine.connect() as c:
        c.execute(text("SET rdkit.tanimoto_threshold = 0.7"))
        for sq in queries["substructure"]:
            q, k = sq["query"], sq["kind"]
            qs = cartridge_query_sql(k)
            uncapped = text(f"SELECT id FROM mols WHERE m @> {qs}")
            capped = text(f"SELECT id FROM mols WHERE m @> {qs} LIMIT 1000")
            count = text(f"SELECT count(*) FROM mols WHERE m @> {qs}")
            hits = int(c.execute(count, {"q": q}).scalar_one())
            rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": False, "hits": hits,
                         "median_s": time_calls(lambda: c.execute(uncapped, {"q": q}).all(), repeats)})
            rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": True, "hits": min(hits, 1000),
                         "median_s": time_calls(lambda: c.execute(capped, {"q": q}).all(), repeats)})
            rows.append({**base, "query_id": sq["id"], "kind": k, "op": "count", "capped": False, "hits": hits,
                         "median_s": time_calls(lambda: c.execute(count, {"q": q}).scalar_one(), repeats)})
        for sq in queries["substructure"][:consumer_path_queries]:
            q, k = sq["query"], sq["kind"]
            stmt = text(f"SELECT id FROM mols WHERE mol_from_smiles(CAST(smiles AS cstring)) @> {cartridge_query_sql(k)}")
            t0 = time.perf_counter()
            n = len(c.execute(stmt, {"q": q}).all())
            rows.append({**base, "query_id": sq["id"], "kind": k, "op": "consumer_path", "capped": False, "hits": n,
                         "median_s": time.perf_counter() - t0, "repeats": 1})
        for sq in queries["similarity"]:
            q = sq["query"]
            sim = text("SELECT id FROM mols WHERE mfp % morganbv_fp(mol_from_smiles(CAST(:q AS cstring)))")
            knn = text("SELECT id FROM mols ORDER BY mfp <%> morganbv_fp(mol_from_smiles(CAST(:q AS cstring))) LIMIT 100")
            hits = len(c.execute(sim, {"q": q}).all())
            rows.append({**base, "query_id": sq["id"], "kind": "smiles", "op": "similar", "capped": False, "hits": hits,
                         "median_s": time_calls(lambda: c.execute(sim, {"q": q}).all(), repeats)})
            rows.append({**base, "query_id": sq["id"], "kind": "smiles", "op": "topk100", "capped": True, "hits": 100,
                         "median_s": time_calls(lambda: c.execute(knn, {"q": q}).all(), repeats)})
    return rows


def summarize(rows: list[dict]) -> str:
    groups: dict[tuple, list[float]] = {}
    for r in rows:
        key = (r["side"], r["where"], r["mode"], r["threads"], r["op"], "capped" if r["capped"] else "uncapped")
        groups.setdefault(key, []).append(r["median_s"])
    lines = ["| side | where | mode | threads | op | cap | queries | p50 s | p95 s |", "|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(groups, key=str):
        xs = groups[key]
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {key[4]} | {key[5]} | {len(xs)} | "
                     f"{percentile(xs, 50):.4f} | {percentile(xs, 95):.4f} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="time one side in one mode")
    p.add_argument("--n", required=True)
    p.add_argument("--side", choices=["molseal", "cartridge"])
    p.add_argument("--mode", choices=["cold", "warm"], default="warm")
    p.add_argument("--where", choices=["vm", "host"], default="host")
    p.add_argument("--threads", type=int, nargs="*", default=[1, -1])
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--consumer-path-queries", type=int, default=5)
    p.add_argument("--prewarm", action="store_true", help="cartridge warm mode: pg_prewarm table and indexes first")
    p.add_argument("--report", action="store_true")
    p.add_argument("--cartridge-url", default=os.environ.get("MOLSEAL_CARTRIDGE_URL", CARTRIDGE_URL))
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""))
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY_FILE", ""))
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""))
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"))
    a = p.parse_args(argv)
    results = Path("bench/results")
    if a.report:
        rows = [r for f in sorted(results.glob(f"timings_{a.n}_*.json")) for r in json.loads(f.read_text())["rows"]]
        md = summarize(rows)
        (results / f"timings_{a.n}.md").write_text(md)
        print(md)
        return 0
    parity = results / f"parity_{a.n}.json"
    if not parity.exists() or not json.loads(parity.read_text()).get("ok"):
        raise SystemExit("parity gates have not passed for this N; refusing to time")
    queries = json.loads(Path(f"bench/data/queries_{a.n}.json").read_text())
    rej = results / f"cartridge_rejected_{a.n}.txt"
    rejected = {uuid.UUID(l) for l in rej.read_text().split()} if rej.exists() else set()
    meta = metadata(a.n, a.where)
    meta["mode"] = a.mode
    rows: list[dict] = []
    if a.side == "molseal":
        t0 = time.perf_counter()
        vault = Vault(SqlStore(sa.create_engine(a.db)), FileKeyProvider(Path(a.key)), uuid.UUID(a.tenant),
                      snapshot_dir=Path(a.snapshot_dir) if a.snapshot_dir else None)
        meta["cold_load_s"] = round(time.perf_counter() - t0, 2)
        snap = Path(a.snapshot_dir, f"{a.tenant}.molseal") if a.snapshot_dir else None
        meta["snapshot_bytes"] = snap.stat().st_size if snap and snap.exists() else None
        for th in a.threads:
            rows += run_molseal(vault, queries, rejected, th, a.repeats, end_to_end=(th == -1))
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        meta["max_rss_mb"] = round(rss / (1024 if sys.platform.startswith("linux") else 1 << 20), 1)
        name = f"timings_{a.n}_molseal_{a.where}_{a.mode}.json"
    else:
        engine = sa.create_engine(a.cartridge_url)
        meta["buffers_before"] = cold_check(engine)
        if a.mode == "cold" and meta["buffers_before"] > 0.01 * 524288:      # 4 GB of 8 KB pages
            raise SystemExit(f"shared buffers are not cold ({meta['buffers_before']} pages); restart the container first")
        if a.prewarm:
            with engine.begin() as c:
                for rel in ("mols", "mols_m_gist", "mols_mfp_gist"):
                    c.execute(text(f"SELECT pg_prewarm('{rel}')"))
        rows = run_cartridge(engine, queries, a.repeats, a.consumer_path_queries)
        meta["container_mem"] = _sh(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", "bench-cartridge-db-1"])
        name = f"timings_{a.n}_cartridge_{a.where}_{a.mode}.json"
    for r in rows:
        r.update({"where": a.where, "mode": a.mode})
    results.mkdir(parents=True, exist_ok=True)
    (results / name).write_text(json.dumps({"meta": meta, "rows": rows}, indent=1))
    print(f"{len(rows)} rows -> {results / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The cartridge container's name under the bench compose project is `bench-cartridge-db-1`; confirm with `docker ps` and adjust the `docker stats` target if it differs. If the `<%>` operator is not available in this cartridge build, drop the `topk100` cartridge rows and say so in the results file's `meta`.

Run: `uv run pytest tests/bench/test_run_bench.py -q`. Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add -- bench/run_bench.py tests/bench/test_run_bench.py
git commit -m "feat(bench): timing runner with cold check, prewarm, labeled modes and report

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/run_bench.py tests/bench/test_run_bench.py
```

---

### Task 8: Bench container and procedure

**Files:**
- Create: `bench/Dockerfile`, `bench/README.md`
- Modify: `bench/compose.yml` (add `molseal-db` and `bench` services)

**Interfaces:**
- Produces: compose profile `molseal` with service `molseal-db` (`postgres:16`, user/password/db `molseal`, `127.0.0.1:5441`, `-c log_statement=none` by default), and profile `bench` with service `bench` built from `bench/Dockerfile`, mounting `./data` and `./results`, with env `MOLSEAL_DATABASE_URL=postgresql+psycopg://molseal:molseal@molseal-db:5432/molseal` and `MOLSEAL_CARTRIDGE_URL=postgresql+psycopg://bench:bench@cartridge-db:5432/bench`.

- [ ] **Step 1: Write the Dockerfile**

`bench/Dockerfile`:
```dockerfile
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git libxrender1 libxext6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY bench ./bench
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python"]
```
The two X11 libraries are what the RDKit wheel needs on a slim image (the same gotcha the ChemCellar backend image hit). Build from the repo root so `src` and `bench` are in context: `docker compose -f bench/compose.yml --profile bench build`.

- [ ] **Step 2: Extend the compose file**

Append to `bench/compose.yml` under `services:`:
```yaml
  molseal-db:
    image: postgres:16
    profiles: ["molseal"]
    environment:
      POSTGRES_USER: molseal
      POSTGRES_PASSWORD: molseal
      POSTGRES_DB: molseal
    command: postgres -c shared_buffers=1GB
    ports:
      - "127.0.0.1:5441:5432"
    volumes:
      - molseal-pgdata:/var/lib/postgresql/data
  bench:
    build:
      context: ..
      dockerfile: bench/Dockerfile
    profiles: ["bench"]
    environment:
      MOLSEAL_DATABASE_URL: postgresql+psycopg://molseal:molseal@molseal-db:5432/molseal
      MOLSEAL_CARTRIDGE_URL: postgresql+psycopg://bench:bench@cartridge-db:5432/bench
      MOLSEAL_KEY_FILE: /app/bench/results/bench.key
      MOLSEAL_SNAPSHOT_DIR: /app/bench/data/snap
    volumes:
      - ./data:/app/bench/data
      - ./results:/app/bench/results
```
and `molseal-pgdata:` under `volumes:`. The bench container joins the compose network, so it reaches whichever database profile is up by service name. `docker` is not available inside the container; the runner's metadata helpers degrade to "unavailable" there, and the host-side driver (Task 9) fills `image_arch` and `docker_vm` into the report instead.

- [ ] **Step 3: Write the procedure**

`bench/README.md` covering, in this order: VM sizing (Docker Desktop, Resources, Memory 16 GB for 500k, 24 GB for full ChEMBL; CPUs all); fetching slices; the 10k gate; starting one database profile at a time; loading each side (host-native loaders are fine, loads are not compared); building the query set; running parity with both profiles up; timing order (cartridge cold after `docker compose -f bench/compose.yml restart cartridge-db`, then cartridge warm with `--prewarm`, then molseal in the VM via `docker compose -f bench/compose.yml --profile molseal --profile bench run --rm bench -m bench.run_bench ...` cold then warm, then molseal host-native); the report command; what every label means (copy the definitions from the spec's 8.5); ChEMBL attribution; and the rule that a run with failed parity publishes nothing.

- [ ] **Step 4: Prove the container works**

Run: `docker compose -f bench/compose.yml --profile bench build` then `docker compose -f bench/compose.yml --profile bench run --rm bench -c "import rdkit, molseal, numpy; print(rdkit.__version__, molseal.__version__, numpy.__version__)"`. Expected: `2025.03.3 0.0.1 2.x`. Put the output in the commit body.

- [ ] **Step 5: Commit**

```bash
git add -- bench/Dockerfile bench/README.md bench/compose.yml
git commit -m "feat(bench): bench container, molseal-db profile and the measurement procedure

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/Dockerfile bench/README.md bench/compose.yml
```

---

### Task 9: The 500k run

**Files:**
- Create: `bench/run_m2.sh`, `docs/results/README.md`, `docs/results/2026-10-<dd>-500k/` (copied JSON and Markdown)
- Modify: `README.md` (a Results section)

**Interfaces:**
- Produces: a driver that runs every step of milestone 2 in order and stops at the first failure, plus the committed results.

- [ ] **Step 1: Write the driver**

`bench/run_m2.sh` (bash, `set -euo pipefail`), with the steps as numbered functions and a `STEP=` variable to resume from a step:
1. `uv run python -m bench.fetch_chembl --n 10000` and `--n 500000`
2. `uv run python -m bench.gate` (exit on failure)
3. `docker compose -f bench/compose.yml --profile molseal up -d`; wait for ready; `uv run python -m bench.load_molseal --slice bench/data/slice_500000.tsv --db postgresql+psycopg://molseal:molseal@localhost:5441/molseal --key bench/results/bench.key --tenant $TENANT --snapshot-dir bench/data/snap` (generate `bench/results/bench.key` and a fixed `TENANT` uuid saved in `bench/results/tenant.txt` on first run)
4. `docker compose -f bench/compose.yml --profile cartridge up -d`; wait; `uv run python -m bench.load_cartridge --slice bench/data/slice_500000.tsv`
5. `uv run python -m bench.queries --slice bench/data/slice_500000.tsv`
6. `uv run python -m bench.parity --n 500000 --db ... --key ... --tenant ... --snapshot-dir bench/data/snap` (both profiles up; exit on failure)
7. `docker compose -f bench/compose.yml --profile molseal stop`; `docker compose -f bench/compose.yml restart cartridge-db`; wait; `uv run python -m bench.run_bench --n 500000 --side cartridge --mode cold --where vm`; then `--mode warm --prewarm`
8. `docker compose -f bench/compose.yml --profile cartridge stop`; `--profile molseal up -d`; `docker compose -f bench/compose.yml --profile molseal --profile bench run --rm bench -m bench.run_bench --n 500000 --side molseal --mode cold --where vm --tenant $TENANT` then `--mode warm`
9. host-native: `uv run python -m bench.run_bench --n 500000 --side molseal --mode warm --where host --db postgresql+psycopg://molseal:molseal@localhost:5441/molseal --key bench/results/bench.key --tenant $TENANT --snapshot-dir bench/data/snap`
10. `uv run python -m bench.run_bench --n 500000 --report`; `uv run molseal leak-report bench/data/slice_500000.tsv.ids.tsv --db ... --key ... --tenant ... | tee bench/results/leak_500000.json`

- [ ] **Step 2: Run it**

Run: `bash bench/run_m2.sh 2>&1 | tee bench/results/run_m2_500000.log`. This takes a while: the loads are minutes each, the cartridge GiST build on the structure column is the long pole, and the unindexed consumer path is minutes per query. If a step fails, fix the cause, resume with `STEP=<n> bash bench/run_m2.sh`, and never edit a results file by hand.

- [ ] **Step 3: Publish the results into the repo**

Copy `bench/results/parity_500000.json`, every `timings_500000_*.json`, `timings_500000.md`, `load_molseal_500000.json`, `load_cartridge_500000.json`, `gate_10k.json`, `leak_500000.json`, `bench/data/queries_500000.json` and `bench/data/slice_500000.meta.json` into `docs/results/2026-10-<dd>-500k/`. Write `docs/results/README.md` with: what each file is, the VM allocation and image architecture from the timing metadata, the git SHA, and the sentence that every number sits next to its label (side, where, mode, threads, cap). Add a short Results section to the root `README.md` that pastes `timings_500000.md` and links the folder, with the labels explained in one line each and the leak-report numbers quoted.

- [ ] **Step 4: Commit**

```bash
git add -- bench/run_m2.sh docs/results README.md
git commit -m "feat(bench): milestone 2 driver and the 500k results

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" -- bench/run_m2.sh docs/results README.md
git tag -a milestone-2 -m "molseal milestone 2: 500k benchmark against the cartridge"
```

---

## After this plan

Milestones 3 and 4 rerun `bench/run_m2.sh` with `N=1000000` and then the full file (`--n 2500000` takes every row), after raising the Docker VM to 24 GB. The write-up draft (spec section 12) is written from `docs/results/` once the full run exists. The deferred minors from milestone 1's review, listed in `docs/superpowers/reviews/2026-10-13-milestone-1-ledger.md`, are triaged in the same pass.
