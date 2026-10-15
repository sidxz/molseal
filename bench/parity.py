"""The parity gates. No timing is published from a run where any gate fails."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import uuid
from itertools import batched
from pathlib import Path

import numpy as np

import sqlalchemy as sa
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from sqlalchemy import text

from bench.load_cartridge import CARTRIDGE_URL
from molseal import engine, metrics
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
from molseal.keys import provider_from_spec
from molseal.store import SqlStore
from molseal.vault import Vault

_GEN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)

FAMILIES = (MORGAN2, MORGAN2_CHIRAL, FEATMORGAN2)
GATE5_THRESHOLDS = (0.55, 0.7)
_Q = "mol_from_smiles(CAST(:q AS cstring))"
SQL_FEAT = (f"SELECT id, tanimoto_sml(fmfp, featmorganbv_fp({_Q}, 2)) AS s FROM mols "
            f"WHERE fmfp IS NOT NULL AND tanimoto_sml(fmfp, featmorganbv_fp({_Q}, 2)) >= :t")
SQL_TVERSKY = (f"SELECT id, tversky_sml(morganbv_fp({_Q}), mfp, CAST(1.0 AS real), CAST(0.0 AS real)) AS s FROM mols "
               f"WHERE mfp IS NOT NULL AND tversky_sml(morganbv_fp({_Q}), mfp, CAST(1.0 AS real), CAST(0.0 AS real)) >= :t")
SQL_DICE = (f"SELECT id, dice_sml(mfp, morganbv_fp({_Q})) AS s FROM mols "
            f"WHERE mfp IS NOT NULL AND dice_sml(mfp, morganbv_fp({_Q})) >= :t")
SQL_GEN_IDS = f"SELECT id FROM mols WHERE m @>> mol_to_xqmol({_Q})"
SQL_GEN_COUNT = f"SELECT count(*) FROM mols WHERE m @>> mol_to_xqmol({_Q})"


def ensure_feature_column(conn) -> None:
    """Gate 5 compares against the cartridge's feature-Morgan fingerprint; the column is added once.
    The cartridge's feature-Morgan size is its own session setting (default 512 bits, separate from
    morgan_fp_size), so it is set here to the library's 2048; the gate-5 queries run on this same
    connection, so the per-query fingerprint is 2048 bits too. Rows filled at another size are redone."""
    conn.execute(text("SET rdkit.featmorgan_fp_size = 2048"))
    conn.execute(text("ALTER TABLE mols ADD COLUMN IF NOT EXISTS fmfp bfp"))
    got = conn.execute(text("SELECT size(featmorganbv_fp(mol_from_smiles(CAST('C' AS cstring)), 2))")).scalar_one()
    if got != 2048:
        raise RuntimeError(f"rdkit.featmorgan_fp_size did not take: featmorganbv_fp gives {got} bits, expected 2048")
    conn.execute(text("UPDATE mols SET fmfp = featmorganbv_fp(m, 2) WHERE m IS NOT NULL AND (fmfp IS NULL OR size(fmfp) <> 2048)"))
    conn.commit()


def _cart_scores(conn, sql: str, query: str, threshold: float) -> dict[uuid.UUID, float]:
    return {r.id: float(r.s) for r in conn.execute(text(sql), {"q": query, "t": threshold})}


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


def check_substructure(qid: str, ms_hits: list, ms_count: int, cart_count: int, rejected: set,
                       cart_ids=None, id_set_limit: int = 10_000) -> tuple[dict, dict]:
    """Gates 2 and 3 for one query. molseal also indexes the rows the cartridge rejected, so those are
    taken out of its hit list and its count. Gate 2: count_substructure, the hit-list length (a list,
    so a duplicate shows) and the cartridge count agree. Gate 3: the id sets agree; skipped, and said
    so, above `id_set_limit` hits. `cart_ids` is called only when gate 3 runs."""
    kept = [i for i in ms_hits if i not in rejected]
    count = ms_count - (len(ms_hits) - len(kept))
    g2 = {"id": qid, "molseal": len(kept), "molseal_count": count, "cartridge": cart_count,
          "ok": len(kept) == count == cart_count}
    if cart_count > id_set_limit:
        return g2, {"id": qid, "skipped": f"count > {id_set_limit}", "ok": True}
    cmp = compare_sets(set(kept), cart_ids())
    return g2, {"id": qid, **cmp, "ok": cmp["equal"] and len(set(kept)) == len(kept)}


def check_similarity(qid: str, ms_pairs: list, cart_scores: dict, src, rejected: set) -> dict:
    """Gate 4 for one query: ids and scores, leaving out the source molecule and rejected rows."""
    ms = {i: s for i, s in ms_pairs if i != src and i not in rejected}
    cs = {i: s for i, s in cart_scores.items() if i != src}
    cmp = compare_scores(ms, cs)
    return {"id": qid, **cmp, "molseal": len(ms), "cartridge": len(cs), "ok": cmp["equal"]}


def check_chiral(qid: str, on_ms: list, on_cart: set, off_ms: list, off_cart: set, rejected: set) -> dict:
    """Gate 8 for one query: chirality-on id sets agree, and chirality-off is a superset on both sides."""
    on = {i for i in on_ms if i not in rejected}
    off = {i for i in off_ms if i not in rejected}
    cmp = compare_sets(on, on_cart)
    return {"id": qid, **cmp, "superset_molseal": on <= off, "superset_cartridge": on_cart <= off_cart,
            "ok": cmp["equal"] and on <= off and on_cart <= off_cart}


def check_rows(found: list, mat: np.ndarray, reference: dict) -> dict:
    """Gate 10 fingerprint bytes: every row handed out equals the row computed by RDKit in Python."""
    mism = sum(1 for i, row in zip(found, mat) if not np.array_equal(row, reference[i]))
    missing = len(reference) - len(found)
    return {"compared": len(found), "mismatches": mism, "missing": missing, "ok": mism == 0 and missing == 0}


def reference_fingerprints(vault: Vault, ids: list[uuid.UUID], gens: dict) -> tuple[list[uuid.UUID], dict[str, list]]:
    """RDKit bit vectors for every live id, computed in Python from the decrypted SMILES (get_many)."""
    found: list[uuid.UUID] = []
    fps: dict[str, list] = {name: [] for name in gens}
    for group in batched(ids, 10_000):
        for i, p in vault.get_many(group).items():
            m = Chem.MolFromSmiles(p.smiles)
            if m is None:
                continue
            found.append(i)
            for name, g in gens.items():
                fps[name].append(g.GetFingerprint(m))
    return found, fps


def overall_ok(out: dict) -> bool:
    lists = tuple(f"gate{n}" for n in range(2, 11))
    return out["gate1"]["ok"] and all(r["ok"] for g in lists for r in out.get(g, []))


def run(vault: Vault, cart_engine: sa.Engine, queries: dict, rejected: set[uuid.UUID], sample_ids: list[uuid.UUID],
        all_ids: list[uuid.UUID], threshold: float = 0.7, id_set_limit: int = 10_000) -> dict:
    out: dict = {"gate1": {}, **{f"gate{n}": [] for n in range(2, 11)}}
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
            q, k = sq["query"], sq["kind"]
            g2, g3 = check_substructure(sq["id"], vault.substructure(q, kind=k), vault.count_substructure(q, kind=k),
                                        _cart_count(conn, k, q), rejected, lambda: _cart_ids(conn, k, q), id_set_limit)
            out["gate2"].append(g2)
            out["gate3"].append(g3)
        # Gate 4: similarity
        for sq in queries["similarity"]:
            out["gate4"].append(check_similarity(sq["id"], vault.similar(sq["query"], threshold=threshold),
                                                 _cart_similar(conn, sq["query"], threshold), uuid.UUID(sq["source_id"]),
                                                 rejected))
        # Gates 5 and 6: another family, and two more metrics, against the cartridge
        ensure_feature_column(conn)
        for sq in queries["similarity"]:
            q, src = sq["query"], uuid.UUID(sq["source_id"])
            for t in GATE5_THRESHOLDS:
                rec = check_similarity(sq["id"], vault.similar(q, t, family="featmorgan2"), _cart_scores(conn, SQL_FEAT, q, t), src, rejected)
                out["gate5"].append({"threshold": t, **rec})
            for name, metric, sql in (("tversky(1, 0)", metrics.tversky(1, 0), SQL_TVERSKY), ("dice", metrics.dice, SQL_DICE)):
                rec = check_similarity(sq["id"], vault.similar(q, threshold, metric=metric), _cart_scores(conn, sql, q, threshold), src, rejected)
                out["gate6"].append({"metric": name, **rec})
        # Gate 7: generalized substructure against @>>; gate 9: molblock self-consistency. SMILES queries only.
        for sq in queries["substructure"]:
            if sq["kind"] != "smiles":
                continue
            q = sq["query"]
            g2, g3 = check_substructure(sq["id"], vault.substructure(q, generalized=True), vault.count_substructure(q, generalized=True),
                                        int(conn.execute(text(SQL_GEN_COUNT), {"q": q}).scalar_one()), rejected,
                                        lambda: {r.id for r in conn.execute(text(SQL_GEN_IDS), {"q": q})}, id_set_limit)
            out["gate7"].append({**g2, "ids": g3, "ok": g2["ok"] and g3["ok"]})
            block = Chem.MolToMolBlock(Chem.MolFromSmiles(q))
            cmp = compare_sets(set(vault.substructure(block, kind="molblock")), set(vault.substructure(q)))
            out["gate9"].append({"id": sq["id"], **cmp, "ok": cmp["equal"]})
        # Gate 8: chirality against rdkit.do_chiral_sss
        for cq in queries.get("chiral", []):
            q = cq["query"]
            conn.execute(text("SET rdkit.do_chiral_sss = true"))
            on_cart = _cart_ids(conn, "smiles", q)
            conn.execute(text("SET rdkit.do_chiral_sss = false"))
            off_cart = _cart_ids(conn, "smiles", q)
            out["gate8"].append(check_chiral(cq["id"], vault.substructure(q, use_chirality=True), on_cart, vault.substructure(q), off_cart, rejected))
    # Gate 10: what the cartridge cannot check, against RDKit in Python. The reference holds the
    # rejected rows too, so nothing is filtered on either side.
    gens = {s.name: s.generator() for s in FAMILIES}
    found, ref = reference_fingerprints(vault, all_ids, gens)
    legs = (("morgan2", "tanimoto", metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
            ("morgan2-chiral", "tanimoto", metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
            ("morgan2", "cosine", metrics.cosine, DataStructs.BulkCosineSimilarity))
    for sq in queries["similarity"]:
        q, src = sq["query"], uuid.UUID(sq["source_id"])
        qm = Chem.MolFromSmiles(q)
        for family, mname, metric, bulk in legs:
            scores = {i: s for i, s in zip(found, bulk(gens[family].GetFingerprint(qm), ref[family])) if s >= threshold}
            rec = check_similarity(sq["id"], vault.similar(q, threshold, family=family, metric=metric), scores, src, set())
            out["gate10"].append({"family": family, "metric": mname, **rec})
    for spec in FAMILIES:
        reference = {i: engine.pack_fingerprints(gens[spec.name], [Chem.MolFromSmiles(vault.get(i).smiles)], spec.nbytes)[0] for i in sample_ids}
        f, mat = vault.fingerprints(sample_ids, spec.name)
        out["gate10"].append({"family": spec.name, "metric": "rows", "id": "sample", **check_rows(f, mat, reference)})
    out["zero_hit_queries"] = [r["id"] for r in out["gate2"] if r["cartridge"] == 0]
    out["ok"] = overall_ok(out)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="run the parity gates")
    p.add_argument("--n", required=True)
    p.add_argument("--cartridge-url", default=os.environ.get("MOLSEAL_CARTRIDGE_URL", CARTRIDGE_URL))
    p.add_argument("--db", default=os.environ.get("MOLSEAL_DATABASE_URL", ""))
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY", ""))
    p.add_argument("--tenant", default=os.environ.get("MOLSEAL_TENANT", ""))
    p.add_argument("--snapshot-dir", default=os.environ.get("MOLSEAL_SNAPSHOT_DIR"))
    p.add_argument("--seed", type=int, default=20261013)
    a = p.parse_args(argv)
    queries = json.loads(Path(f"bench/data/queries_{a.n}.json").read_text())
    rej_path = Path(f"bench/results/cartridge_rejected_{a.n}.txt")
    rejected = {uuid.UUID(l) for l in rej_path.read_text().split()} if rej_path.exists() else set()
    store = SqlStore(sa.create_engine(a.db))
    vault = Vault(store, provider_from_spec(a.key), uuid.UUID(a.tenant), snapshot_dir=Path(a.snapshot_dir) if a.snapshot_dir else None,
                  families=FAMILIES)
    sidecar = Path(f"bench/data/slice_{a.n}.tsv.ids.tsv")
    ids = [uuid.UUID(l.split("\t")[0]) for l in sidecar.read_text().splitlines()]
    sample_ids = [i for i in random.Random(a.seed).sample(ids, min(100, len(ids))) if i not in rejected]
    result = run(vault, sa.create_engine(a.cartridge_url), queries, rejected, sample_ids, ids)
    if a.snapshot_dir:
        vault.save_snapshot()
    out = Path(f"bench/results/parity_{a.n}.json")
    out.write_text(json.dumps(result, indent=1))
    bad = [(g, r["id"]) for g in (f"gate{n}" for n in range(2, 11)) for r in result[g] if not r["ok"]]
    print(f"parity {'OK' if result['ok'] else 'FAILED'}: gate1 mismatches {result['gate1']['mismatches']}, failing queries {bad[:10]} -> {out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
