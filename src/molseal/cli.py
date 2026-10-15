"""Command line for the proof of concept.

The CLI acts as a caller of the library: it is the CLI that decides the example exact key is
the InChIKey. Nothing under molseal/ except this file computes one.
"""

from __future__ import annotations

import argparse
import itertools
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
from rdkit import Chem, RDLogger
from sqlalchemy import delete, func, insert, select

from .keys import FileKeyProvider, generate_key_file
from .store import SqlStore, structure_keys, structures, tenant_keys
from .vault import Vault

RDLogger.DisableLog("rdApp.*")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""), help="SQLAlchemy URL [env MOLSEAL_DATABASE_URL]")
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY_FILE", ""), help="KEK file [env MOLSEAL_KEY_FILE]")
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""), help="tenant UUID [env MOLSEAL_TENANT]")
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"), help="[env MOLSEAL_SNAPSHOT_DIR]")


def _require(args: argparse.Namespace) -> None:
    missing = [f"--{n}" for n in ("db", "key", "tenant") if not getattr(args, n)]
    if missing:
        raise SystemExit(f"required: {' '.join(missing)} (or their MOLSEAL_* environment variables)")


def _store(args: argparse.Namespace) -> tuple[SqlStore, uuid.UUID]:
    _require(args)
    store = SqlStore(sa.create_engine(args.db))
    store.create_tables()
    return store, uuid.UUID(args.tenant)


def _vault(args: argparse.Namespace) -> Vault:
    store, tenant = _store(args)
    snap = Path(args.snapshot_dir) if args.snapshot_dir else None
    return Vault(store, FileKeyProvider(Path(args.key)), tenant, snapshot_dir=snap)


def cmd_keygen(args: argparse.Namespace) -> int:
    try:
        generate_key_file(Path(args.path))
    except FileExistsError:
        print(f"refusing to overwrite {args.path}", file=sys.stderr)
        return 1
    print(f"wrote {args.path} (mode 600)")
    return 0


LOAD_CHUNK = 10_000


def _parse_smiles_file(src: Path, stats: dict[str, int]):
    """Yield (id, canonical_smiles, inchikey) for each parseable line; count the rest in stats["skipped"]."""
    stats["skipped"] = 0
    with open(src) as fin:
        for line in fin:
            tok = line.split()
            if not tok or tok[0].startswith("#"):
                continue
            mol = Chem.MolFromSmiles(tok[0])
            if mol is None:
                stats["skipped"] += 1
                continue
            yield uuid.uuid4(), Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)


def cmd_load(args: argparse.Namespace) -> int:
    vault = _vault(args)
    src = Path(args.file)
    sidecar = src.with_name(src.name + ".ids.tsv")
    loaded = 0
    stats: dict[str, int] = {}
    rows = _parse_smiles_file(src, stats)
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
    print(f"loaded {loaded}, skipped {stats['skipped']}, sidecar {sidecar}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    vault = _vault(args)
    kind = "smarts" if args.smarts else "smiles"
    for id_ in vault.substructure(args.query, kind=kind, max_results=args.max):
        print(id_)
    return 0


def cmd_count(args: argparse.Namespace) -> int:
    vault = _vault(args)
    print(vault.count_substructure(args.query, kind="smarts" if args.smarts else "smiles"))
    return 0


def cmd_similar(args: argparse.Namespace) -> int:
    vault = _vault(args)
    for id_, score in vault.similar(args.smiles, threshold=args.threshold, top_k=args.top):
        print(f"{id_}\t{score:.4f}")
    return 0


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
            .where(structures.c.tenant_id == tenant, structures.c.deleted == False))}  # noqa: E712
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="molseal")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="write a new 32-byte key file with mode 600")
    k.add_argument("path")
    k.set_defaults(fn=cmd_keygen)

    l = sub.add_parser("load", help="load a SMILES file (one per line); writes FILE.ids.tsv")
    l.add_argument("file")
    _add_common(l)
    l.set_defaults(fn=cmd_load)

    s = sub.add_parser("search", help="substructure search; prints ids")
    s.add_argument("query")
    s.add_argument("--smarts", action="store_true")
    s.add_argument("--max", type=int, default=None)
    _add_common(s)
    s.set_defaults(fn=cmd_search)

    c = sub.add_parser("count", help="substructure count")
    c.add_argument("query")
    c.add_argument("--smarts", action="store_true")
    _add_common(c)
    c.set_defaults(fn=cmd_count)

    m = sub.add_parser("similar", help="Tanimoto similarity; prints id<TAB>score")
    m.add_argument("smiles")
    m.add_argument("--threshold", type=float, default=0.7)
    m.add_argument("--top", type=int, default=None)
    _add_common(m)
    m.set_defaults(fn=cmd_similar)

    a = sub.add_parser("absence-check", help="prove no loaded structure appears in the DB, a plain dump, or a log")
    a.add_argument("sidecar")
    a.add_argument("--dump", help="path to a `pg_dump -Fp --compress=0` output to scan")
    a.add_argument("--run-pg-dump", action="store_true", help="run pg_dump against --db and scan its output")
    a.add_argument("--log", help="PostgreSQL log captured with log_statement=all")
    _add_common(a)
    a.set_defaults(fn=cmd_absence_check)

    r = sub.add_parser("leak-report", help="how much ciphertext size class says about molecule size")
    r.add_argument("sidecar")
    _add_common(r)
    r.set_defaults(fn=cmd_leak_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except SystemExit as exc:          # _require() raises with a message; argparse raises with an int
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
