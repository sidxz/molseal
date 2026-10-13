"""Command line for the proof of concept.

The CLI acts as a caller of the library: it is the CLI that decides the example exact key is
the InChIKey. Nothing under molseal/ except this file computes one.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

import sqlalchemy as sa
from rdkit import Chem, RDLogger

from .keys import FileKeyProvider, generate_key_file
from .store import SqlStore
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


def cmd_load(args: argparse.Namespace) -> int:
    vault = _vault(args)
    src = Path(args.file)
    sidecar = src.with_name(src.name + ".ids.tsv")
    loaded = skipped = 0
    with open(src) as fin, open(sidecar, "a") as fout:
        for line in fin:
            tok = line.split()
            if not tok or tok[0].startswith("#"):
                continue
            mol = Chem.MolFromSmiles(tok[0])
            if mol is None:
                skipped += 1
                continue
            canonical, inchikey = Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)
            id_ = uuid.uuid4()
            vault.put(id_, canonical, {"inchikey": inchikey})
            fout.write(f"{id_}\t{canonical}\t{inchikey}\n")
            loaded += 1
            if loaded % 1000 == 0:
                print(f"{loaded} loaded", file=sys.stderr)
    if args.snapshot_dir:
        vault.save_snapshot()
    print(f"loaded {loaded}, skipped {skipped}, sidecar {sidecar}")
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
