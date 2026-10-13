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
        # fallback if psycopg rejects uuid.UUID list: ids=[str(i)...] with id = ANY(CAST(:ids AS uuid[]))
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
