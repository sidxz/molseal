"""Command line for the proof of concept.

The CLI acts as a caller of the library: it is the CLI that decides the example exact key is the
InChIKey. Nothing under molseal/ except this file computes one. The evaluation commands (absence
check, leak report) live in bench/leak.py.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import get_args

import sqlalchemy as sa
from rdkit import Chem, RDLogger

from .engine import QueryKind
from .keys import (KeyProvider, KeyProviderError, generate_key_file, new_tenant_secret, provider_from_spec,
                   unwrap_tenant_key, wrap_tenant_key)
from .store import SqlStore
from .vault import Vault, VaultError, rewrap_tenant

RDLogger.DisableLog("rdApp.*")      # the CLI owns its process; the library never silences RDKit


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""), help="SQLAlchemy URL [env MOLSEAL_DATABASE_URL]")
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY", ""),
                   help="key provider: PATH | file:PATH | aws:KEY_ID | gcp:KEY_NAME | azure:KEY_URL | "
                        "infisical:BASE_URL/KEY_ID | vault:BASE_URL/MOUNT/KEY [env MOLSEAL_KEY]")
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""), help="tenant UUID [env MOLSEAL_TENANT]")
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"), help="[env MOLSEAL_SNAPSHOT_DIR]")


def _add_query_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", choices=get_args(QueryKind), default="smiles", help="query language [smiles]")
    p.add_argument("--generalized", action="store_true", help="match tautomers and enumerated forms")
    p.add_argument("--chirality", action="store_true", help="make the query's stereo mandatory")


def _require(args: argparse.Namespace, names: tuple[str, ...] = ("db", "key", "tenant")) -> None:
    missing = [f"--{n}" for n in names if not getattr(args, n)]
    if missing:
        raise SystemExit(f"required: {' '.join(missing)} (or their MOLSEAL_* environment variables)")


def _store(args: argparse.Namespace) -> tuple[SqlStore, uuid.UUID]:
    _require(args)
    store = SqlStore(sa.create_engine(args.db))
    store.create_tables()
    return store, uuid.UUID(args.tenant)


def _provider(spec: str) -> KeyProvider:
    try:
        return provider_from_spec(spec)
    except KeyProviderError as exc:
        raise SystemExit(str(exc)) from exc        # a usage error: exit 2 like a missing option


def _vault(args: argparse.Namespace) -> Vault:
    store, tenant = _store(args)
    snap = Path(args.snapshot_dir) if args.snapshot_dir else None
    return Vault(store, _provider(args.key), tenant, snapshot_dir=snap)


def cmd_keygen(args: argparse.Namespace) -> int:
    try:
        generate_key_file(Path(args.path))
    except FileExistsError:
        print(f"refusing to overwrite {args.path}", file=sys.stderr)
        return 1
    print(f"wrote {args.path} (mode 600)")
    return 0


def cmd_rewrap(args: argparse.Namespace) -> int:
    """Re-wrap tenant keys: --key is the old provider, --to the new one."""
    _require(args, ("db", "key") if args.all else ("db", "key", "tenant"))
    old, new = _provider(args.key), _provider(args.to)
    store = SqlStore(sa.create_engine(args.db))
    store.create_tables()
    targets = store.list_tenants() if args.all else [uuid.UUID(args.tenant)]
    if args.all and not targets:
        print("no tenants", file=sys.stderr)
        return 0
    failed = False
    for t in targets:
        try:
            n = rewrap_tenant(store, t, old, new)
        except (KeyProviderError, VaultError) as exc:
            print(f"{t}: {exc}", file=sys.stderr)
            if not args.all:
                return 1
            failed = True
            continue
        print(f"{t}: rewrapped {n} key rows")
    return 1 if failed else 0


def cmd_key_check(args: argparse.Namespace) -> int:
    """Wrap and unwrap a throwaway secret through --key; a wrong version and a flipped byte must fail."""
    _require(args, ("key",))
    provider = _provider(args.key)
    name = type(provider).__name__
    t, secret = uuid.uuid4(), new_tenant_secret()
    try:
        blob = wrap_tenant_key(provider, t, "data", 1, secret)
        if unwrap_tenant_key(provider, t, "data", 1, blob) != secret:
            raise KeyProviderError("round trip returned different bytes")
    except KeyProviderError as exc:
        print(f"{name}: {exc}", file=sys.stderr)
        return 1
    for what, version, b in (("wrong version", 2, blob), ("flipped byte", 1, bytes([blob[0] ^ 1]) + blob[1:])):
        try:
            unwrap_tenant_key(provider, t, "data", version, b)
        except KeyProviderError:
            continue
        print(f"{name}: unwrap with a {what} did not fail", file=sys.stderr)
        return 1
    print(f"{name}: ok")
    return 0


LOAD_CHUNK = 10_000


def _parse_smiles_file(src: Path) -> tuple[list[tuple[uuid.UUID, str, str]], int]:
    """(id, canonical_smiles, inchikey) per parseable line, and how many lines RDKit rejected. Blank
    lines and `#` comments are neither loaded nor counted."""
    rows: list[tuple[uuid.UUID, str, str]] = []
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
            rows.append((uuid.uuid4(), Chem.MolToSmiles(mol), Chem.MolToInchiKey(mol)))
    return rows, skipped


def cmd_load(args: argparse.Namespace) -> int:
    vault = _vault(args)
    src = Path(args.file)
    sidecar = src.with_name(src.name + ".ids.tsv")
    rows, skipped = _parse_smiles_file(src)
    with open(sidecar, "a") as fout:
        for start in range(0, len(rows), LOAD_CHUNK):
            chunk = rows[start : start + LOAD_CHUNK]
            vault.put_many([(id_, smi, {"inchikey": key}) for id_, smi, key in chunk])
            for id_, smi, key in chunk:          # sidecar written only after the chunk committed
                fout.write(f"{id_}\t{smi}\t{key}\n")
            print(f"{start + len(chunk)} loaded", file=sys.stderr)
    if args.snapshot_dir:
        vault.save_snapshot()
    print(f"loaded {len(rows)}, skipped {skipped}, sidecar {sidecar}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    vault = _vault(args)
    for id_ in vault.substructure(args.query, kind=args.kind, max_results=args.max,
                                  generalized=args.generalized, use_chirality=args.chirality):
        print(id_)
    return 0


def cmd_count(args: argparse.Namespace) -> int:
    vault = _vault(args)
    print(vault.count_substructure(args.query, kind=args.kind, generalized=args.generalized,
                                   use_chirality=args.chirality))
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
    s.add_argument("--max", type=int, default=None)
    _add_query_options(s)
    _add_common(s)
    s.set_defaults(fn=cmd_search)

    c = sub.add_parser("count", help="substructure count")
    c.add_argument("query")
    _add_query_options(c)
    _add_common(c)
    c.set_defaults(fn=cmd_count)

    m = sub.add_parser("similar", help="Tanimoto similarity; prints id<TAB>score")
    m.add_argument("smiles")
    m.add_argument("--threshold", type=float, default=0.7)
    m.add_argument("--top", type=int, default=None)
    _add_common(m)
    m.set_defaults(fn=cmd_similar)

    w = sub.add_parser("rewrap", help="re-wrap tenant keys under another provider (KEK rotation or migration)")
    w.add_argument("--to", required=True, help="new key provider spec; --key is the old one")
    w.add_argument("--all", action="store_true", help="every tenant in the database instead of --tenant")
    _add_common(w)
    w.set_defaults(fn=cmd_rewrap)

    kc = sub.add_parser("key-check", help="wrap and unwrap a throwaway secret through --key")
    _add_common(kc)
    kc.set_defaults(fn=cmd_key_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except KeyProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except SystemExit as exc:          # _require() raises with a message; argparse raises with an int
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
