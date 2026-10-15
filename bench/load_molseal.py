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

from molseal.keys import KeyProvider, provider_from_spec
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
    done = 0
    if progress_path.exists():
        prog = json.loads(progress_path.read_text())
        if prog.get("chunk") != chunk:
            raise SystemExit(f"{progress_path} was written with chunk {prog.get('chunk')}, not {chunk}; "
                             "resume with the same --chunk or delete it and its sidecar to start over")
        done = prog["rows_done"]
    if sidecar.exists():          # drop sidecar lines written after the last recorded commit
        lines = sidecar.read_text().splitlines(keepends=True)
        if len(lines) != done:
            sidecar.write_text("".join(lines[:done]))
    vault = Vault(store, key_provider, tenant_id, snapshot_dir=snapshot_dir)
    rows = _rows(slice_path)
    next(itertools.islice(rows, done, done), None)       # skip the rows already recorded as committed
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
            side.flush()
            chunks += 1
            loaded += len(group)
            progress_path.write_text(json.dumps({"rows_done": done + loaded, "chunk": chunk}))
            print(f"chunk {chunks}: {done + loaded} rows done, {loaded} this run", file=sys.stderr)
    seconds = time.perf_counter() - t0
    if snapshot_dir is not None:
        vault.save_snapshot()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_div = 1 << 20 if sys.platform == "darwin" else 1024     # bytes on macOS, kilobytes on Linux
    return {"rows": loaded, "chunks": chunks, "resumed_from_row": done, "seconds": round(seconds, 2),
            "rows_per_second": round(loaded / seconds, 1) if seconds > 0 and loaded else 0.0,
            "max_rss_mb": round(rss / rss_div, 1)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="load a slice into molseal")
    p.add_argument("--slice", required=True)
    p.add_argument("--chunk", type=int, default=10_000)
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"))
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""))
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY", ""))
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""))
    p.add_argument("--results", default="bench/results")
    a = p.parse_args(argv)
    if not (a.db and a.key and a.tenant):
        raise SystemExit("--db, --key and --tenant (or MOLSEAL_* env vars) are required")
    store = SqlStore(sa.create_engine(a.db))
    store.create_tables()
    stats = load(Path(a.slice), store, provider_from_spec(a.key), uuid.UUID(a.tenant), a.chunk,
                 Path(a.snapshot_dir) if a.snapshot_dir else None)
    n = Path(a.slice).stem.split("_")[-1]
    out = Path(a.results) / f"load_molseal_{n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**stats, "slice": a.slice, "tenant": a.tenant}, indent=1))
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
