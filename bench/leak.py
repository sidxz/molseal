"""What the database reveals: the absence check and the leak report.

    python -m bench.leak absence-check SIDECAR [--run-pg-dump] [--dump PATH] [--log PATH]
    python -m bench.leak leak-report SIDECAR

Both read the `FILE.ids.tsv` sidecar that `molseal load` writes and talk to the database directly,
without the tenant's key. The absence check plants a control row in the tenant's table, so run it
against an evaluation tenant, not a production one.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sqlalchemy as sa
from rdkit import Chem
from sqlalchemy import delete, func, insert, select

from molseal.store import SqlStore, structure_keys, structures, tenant_keys

MIN_NEEDLE = 8
CONTROL_ID = uuid.UUID("00000000-0000-0000-0000-0000000c0de0")


def _read_sidecar(path: Path) -> list[tuple[uuid.UUID, str, str]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        id_, smiles, key = line.split("\t")
        rows.append((uuid.UUID(id_), smiles, key))
    return rows


def _column_bytes(store: SqlStore, tenant: uuid.UUID) -> bytes:
    with store.engine.connect() as conn:
        parts = [r.nonce + r.ciphertext for r in conn.execute(
            select(structures.c.nonce, structures.c.ciphertext).where(structures.c.tenant_id == tenant))]
        parts += [r.wrapped for r in conn.execute(
            select(tenant_keys.c.wrapped).where(tenant_keys.c.tenant_id == tenant))]
        parts += [r.hmac for r in conn.execute(
            select(structure_keys.c.hmac).where(structure_keys.c.tenant_id == tenant))]
    return b"\x00".join(parts)


def control_marker(tenant: uuid.UUID) -> bytes:
    """The planted control value. Fixed per tenant, so a dump taken after an earlier run carries it."""
    return b"MOLSEAL-CONTROL-PLAINTEXT-" + tenant.hex.encode()


def _found(blob: bytes, lower: bytes, needle: bytes) -> bool:
    """Raw bytes, or the hex digits PostgreSQL prints for bytea (`\\x...` in pg_dump COPY data and
    statement logs). Hex is matched case-insensitively against a lowercased copy of the blob."""
    return needle in blob or needle.hex().encode() in lower


def _scan(blob: bytes, needles: list[bytes], control: bytes) -> tuple[int, bool]:
    lower = blob.lower()
    return sum(_found(blob, lower, n) for n in needles), _found(blob, lower, control)


def _pg_dump(db_url: str) -> bytes | None:
    if shutil.which("pg_dump") is None:
        print("pg_dump not found on PATH; the dump leg did not run", file=sys.stderr)
        return None
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    try:
        return subprocess.run(["pg_dump", "-Fp", "--compress=0", "--dbname", url], capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        print(f"pg_dump failed: {exc.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        return None


def _store(args: argparse.Namespace) -> tuple[SqlStore, uuid.UUID]:
    if not args.db or not args.tenant:
        raise SystemExit("required: --db and --tenant (or MOLSEAL_DATABASE_URL and MOLSEAL_TENANT)")
    store = SqlStore(sa.create_engine(args.db))
    store.create_tables()
    return store, uuid.UUID(args.tenant)


def cmd_absence_check(args: argparse.Namespace) -> int:
    store, tenant = _store(args)
    rows = _read_sidecar(Path(args.sidecar))
    raw = {s for _, s, _ in rows} | {k for _, _, k in rows}
    needles = [n.encode() for n in raw if len(n) >= MIN_NEEDLE]
    skipped_short = len(raw) - len(needles)

    # The control row stays planted: seq 0 and tombstoned, so no vault ever reads it, and a dump taken
    # after any earlier run carries it. Each leg that can see it must find it, or the leg proves nothing.
    control = control_marker(tenant)
    with store.engine.begin() as conn:      # raw write: no cursor bump, no live-count change
        conn.execute(delete(structures).where(structures.c.tenant_id == tenant, structures.c.id == CONTROL_ID))
        conn.execute(insert(structures).values(tenant_id=tenant, id=CONTROL_ID, row_version=1, data_key_version=1,
                                               nonce=b"\x00" * 12, ciphertext=control, deleted=True, seq=0,
                                               updated_at=datetime.now(timezone.utc)))
    db_hits, db_control = _scan(_column_bytes(store, tenant), needles, control)
    ok = db_hits == 0 and db_control

    dump_hits = dump_control = None
    if args.run_pg_dump or args.dump:
        dump = _pg_dump(args.db) if args.run_pg_dump else Path(args.dump).read_bytes()
        if dump is None:
            ok = False
        else:
            dump_hits, dump_control = _scan(dump, needles, control)
            ok = ok and dump_hits == 0 and dump_control

    log_hits = None
    if args.log:      # captured before this run, so it cannot hold the control
        log_hits, _ = _scan(Path(args.log).read_bytes(), needles, control)
        ok = ok and log_hits == 0

    print(json.dumps({"needles": len(needles), "skipped_short": skipped_short, "db_hits": db_hits,
                      "dump_hits": dump_hits, "log_hits": log_hits,
                      "control_found": {"db": db_control, "dump": dump_control},
                      "log_control": "not applicable", "ok": ok}, indent=1))
    return 0 if ok else 1


def cmd_leak_report(args: argparse.Namespace) -> int:
    store, tenant = _store(args)
    rows = _read_sidecar(Path(args.sidecar))
    heavy = {id_: Chem.MolFromSmiles(s).GetNumHeavyAtoms() for id_, s, _ in rows}
    with store.engine.connect() as conn:
        sizes = {r.id: r.n for r in conn.execute(
            select(structures.c.id, func.length(structures.c.ciphertext).label("n"))
            .where(structures.c.tenant_id == tenant, structures.c.deleted == sa.false()))}
    ids = [i for i in heavy if i in sizes]
    cls = np.array([int(math.log2(sizes[i] - 16)) for i in ids])     # ciphertext = size class + 16-byte tag
    atoms = np.array([heavy[i] for i in ids])
    bucket = atoms // 10
    r = float(np.corrcoef(cls, atoms)[0, 1]) if len(ids) > 1 and cls.std() > 0 else 0.0
    predicted = np.empty_like(bucket)
    for c in np.unique(cls):
        mask = cls == c
        predicted[mask] = np.bincount(bucket[mask]).argmax()
    report = {
        "n": len(ids), "pearson_r": round(r, 4),
        "predict_accuracy": round(float((predicted == bucket).mean()), 4),
        "baseline_accuracy": round(float(np.bincount(bucket).max() / len(bucket)), 4),
        "size_classes": {str(2 ** int(c)): int((cls == c).sum()) for c in np.unique(cls)},
    }
    print(json.dumps(report, indent=1))
    return 0


def _add_db(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""), help="SQLAlchemy URL [env MOLSEAL_DATABASE_URL]")
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""), help="tenant UUID [env MOLSEAL_TENANT]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bench.leak")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("absence-check", help="prove no loaded structure appears in the DB, a plain dump, or a log")
    a.add_argument("sidecar")
    a.add_argument("--dump", help="path to a `pg_dump -Fp --compress=0` output to scan")
    a.add_argument("--run-pg-dump", action="store_true", help="run pg_dump against --db and scan its output")
    a.add_argument("--log", help="PostgreSQL log captured with log_statement=all")
    _add_db(a)
    a.set_defaults(fn=cmd_absence_check)

    r = sub.add_parser("leak-report", help="how much ciphertext size class says about molecule size")
    r.add_argument("sidecar")
    _add_db(r)
    r.set_defaults(fn=cmd_leak_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
