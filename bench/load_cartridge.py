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
INSERT = text("INSERT INTO mols (id, chembl_id, smiles, m) VALUES (:id, :cid, :smi, mol_from_smiles(CAST(:smic AS cstring))) "
              "ON CONFLICT (id) DO NOTHING")


def ensure_schema(engine: sa.Engine) -> None:
    with engine.begin() as c:
        for stmt in SCHEMA:
            c.execute(text(stmt))


def _rows(slice_path: Path):
    with open(slice_path) as f:
        for line in f:
            cid, smi, _ = line.rstrip("\n").split("\t")
            yield {"id": row_id(cid), "cid": cid, "smi": smi, "smic": smi}


def _timed(engine: sa.Engine, stmt: str) -> float:
    t0 = time.perf_counter()
    with engine.begin() as c:
        c.execute(text(stmt))
    return round(time.perf_counter() - t0, 2)


def load(slice_path: Path, engine: sa.Engine, batch: int = 5000, results_dir: Path = Path("bench/results")) -> dict:
    """Load into an empty `mols` table. A non-empty table or existing GiST indexes would turn the inserts
    into ON CONFLICT no-ops and the builds into near-zero times, so both refuse (reset the volume)."""
    ensure_schema(engine)
    with engine.connect() as c:
        existing = c.execute(text("SELECT count(*) FROM mols")).scalar_one()
        indexes = c.execute(text("SELECT count(*) FROM pg_indexes WHERE tablename = 'mols' "
                                 "AND schemaname = current_schema() AND indexname LIKE 'mols_%_gist'")).scalar_one()
    if existing or indexes:
        raise SystemExit(f"mols is not empty ({existing} rows, {indexes} GiST indexes); reset the cartridge volume")
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
    with engine.connect() as c:
        inserted = c.execute(text("SELECT count(*) FROM mols")).scalar_one()
    if inserted != n:            # rejected rows are inserted too, with m NULL
        raise SystemExit(f"inserted {inserted} rows, expected {n}")
    fp_seconds = _timed(engine, "UPDATE mols SET mfp = morganbv_fp(m) WHERE mfp IS NULL AND m IS NOT NULL")
    gist_m = _timed(engine, "CREATE INDEX mols_m_gist ON mols USING gist (m)")
    gist_mfp = _timed(engine, "CREATE INDEX mols_mfp_gist ON mols USING gist (mfp)")
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
