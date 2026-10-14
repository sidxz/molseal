"""Milestone 2's first gate: load a 10k slice, then prove absence in the DB, a real pg_dump and the
log_statement=all statement log (bench/run_m2.sh step 2 starts `db` that way), then the leak report."""

from __future__ import annotations

import contextlib
import io
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


def capture_log(dest: Path) -> int:
    """The db container's log so far; returns how many statements it holds, so an empty log fails."""
    out = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "db"], check=True, capture_output=True).stdout
    dest.write_bytes(out)
    return out.count(b"statement: ") + out.count(b"execute ")


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
    # fresh tenant per run: drop any stale sidecar/progress so a re-run never resumes an old load
    slice_path.with_name(slice_path.name + ".ids.tsv").unlink(missing_ok=True)
    progress = RESULTS / "gate.progress.json"
    progress.unlink(missing_ok=True)
    stats = load(slice_path, store, FileKeyProvider(key), tenant, progress_path=progress)
    sidecar = str(slice_path.with_name(slice_path.name + ".ids.tsv"))
    common = ["--db", DB, "--key", str(key), "--tenant", str(tenant)]
    log_path = RESULTS / "gate_10k_pg.log"
    log_statements = capture_log(log_path)                        # before the control row is planted
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc1 = cli.main(["absence-check", sidecar, "--log", str(log_path), *common])   # plants the control row
    print(buf.getvalue(), end="")
    log_hits = json.loads(buf.getvalue())["log_hits"]
    run_dump(RESULTS / "dump_10k.sql")
    rc2 = cli.main(["absence-check", sidecar, "--dump", str(RESULTS / "dump_10k.sql"), *common])
    rc3 = cli.main(["leak-report", sidecar, *common])
    ok = rc1 == 0 and rc2 == 0 and rc3 == 0 and log_statements > 0
    (RESULTS / "gate_10k.json").write_text(json.dumps({"load": stats, "absence_db": rc1, "absence_dump": rc2,
                                                      "log_statements": log_statements, "log_hits": log_hits,
                                                      "leak_report": rc3, "ok": ok}, indent=1))
    print("GATE OK" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
