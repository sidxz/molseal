"""Timings for both sides, with the labels that keep the comparison honest."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
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
from molseal.keys import provider_from_spec
from molseal.store import SqlStore
from molseal.vault import Vault


median = statistics.median


def percentile(xs: list[float], p: float) -> float:
    """Nearest rank on the sorted values (index round(p% * (n - 1))). Used for p95; p50 is the true median."""
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
        found = idx.substructure(q, k, None, threads)
        hits = sum(i not in rejected for i in found)
        count = idx.count_substructure(q, k, threads) - (len(found) - hits)   # rejected rows out, as in parity
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": False, "hits": hits,
                     "median_s": time_calls(lambda: idx.substructure(q, k, None, threads), repeats)})
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "substructure", "capped": True, "hits": min(hits, 1000),
                     "median_s": time_calls(lambda: idx.substructure(q, k, 1000, threads), repeats)})
        rows.append({**base, "query_id": sq["id"], "kind": k, "op": "count", "capped": False, "hits": count,
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
                     f"{median(xs):.4f} | {percentile(xs, 95):.4f} |")
    return "\n".join(lines) + "\n"


def check_parity(results: Path, n: str) -> None:
    """Refuse to time unless parity passed for this N, and passed after the latest load."""
    parity = results / f"parity_{n}.json"
    if not parity.exists() or not json.loads(parity.read_text()).get("ok"):
        raise SystemExit("parity gates have not passed for this N; refusing to time")
    loads = [f.stat().st_mtime for f in results.glob(f"load_*_{n}.json")]
    if loads and parity.stat().st_mtime < max(loads):
        raise SystemExit("parity file is older than the latest load for this N; rerun parity first")


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
    p.add_argument("--key", default=os.environ.get("MOLSEAL_KEY", ""))
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
    check_parity(results, a.n)
    queries = json.loads(Path(f"bench/data/queries_{a.n}.json").read_text())
    rej = results / f"cartridge_rejected_{a.n}.txt"
    rejected = {uuid.UUID(l) for l in rej.read_text().split()} if rej.exists() else set()
    meta = metadata(a.n, a.where)
    meta["mode"] = a.mode
    rows: list[dict] = []
    if a.side == "molseal":
        t0 = time.perf_counter()
        vault = Vault(SqlStore(sa.create_engine(a.db)), provider_from_spec(a.key), uuid.UUID(a.tenant),
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
        with engine.connect() as c:
            meta["max_parallel_workers_per_gather"] = c.execute(text("SHOW max_parallel_workers_per_gather")).scalar_one()
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
