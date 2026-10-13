"""Fetch ChEMBL chemreps, take the first N rows, canonicalize with RDKit in a process pool.

Not part of the molseal package. Data goes under bench/data (git-ignored) and is never redistributed.
ChEMBL is CC BY-SA 3.0: https://www.ebi.ac.uk/chembl/
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.request
from collections.abc import Iterator
from multiprocessing import Pool
from pathlib import Path

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

CHEMBL_URL = "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/chembl_37_chemreps.txt.gz"
POOL_CHUNK = 2000


def download(url: str, dest: Path) -> Path:
    dest = Path(dest)
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if done % (50 << 20) < (1 << 20):
                print(f"{done >> 20} MiB", file=sys.stderr)
    tmp.replace(dest)
    return dest


def iter_chemreps(path: Path) -> Iterator[tuple[str, str, str]]:
    with gzip.open(path, "rt") as f:
        header = f.readline().rstrip("\n").split("\t")
        i_id, i_smi, i_key = header.index("chembl_id"), header.index("canonical_smiles"), header.index("standard_inchi_key")
        for line in f:
            cols = line.rstrip("\n").split("\t")
            yield cols[i_id], cols[i_smi], cols[i_key]


def canonicalize_rows(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str] | None]:
    out = []
    for chembl_id, smiles, key in rows:
        mol = Chem.MolFromSmiles(smiles)
        out.append(None if mol is None else (chembl_id, Chem.MolToSmiles(mol), key))
    return out


def _chunks(it: Iterator, size: int) -> Iterator[list]:
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) == size:
            yield buf
            buf = []
    if buf:
        yield buf


def build_slice(src: Path, n: int, out: Path, workers: int) -> dict:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = iter_chemreps(src)
    first_n = (r for i, r in zip(range(n), rows))
    loaded = skipped = 0
    with open(out, "w") as f:
        if workers > 1:
            with Pool(workers) as pool:
                results = pool.imap(canonicalize_rows, _chunks(first_n, POOL_CHUNK))
                for batch in results:
                    for r in batch:
                        if r is None:
                            skipped += 1
                        else:
                            f.write("\t".join(r) + "\n")
                            loaded += 1
        else:
            for batch in _chunks(first_n, POOL_CHUNK):
                for r in canonicalize_rows(batch):
                    if r is None:
                        skipped += 1
                    else:
                        f.write("\t".join(r) + "\n")
                        loaded += 1
    meta = {"requested": n, "loaded": loaded, "skipped": skipped}
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="fetch ChEMBL chemreps and build a canonical slice")
    p.add_argument("--n", type=int, required=True)
    p.add_argument("--data", default="bench/data")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--url", default=CHEMBL_URL)
    a = p.parse_args(argv)
    data = Path(a.data)
    src = download(a.url, data / Path(a.url).name)
    out = data / f"slice_{a.n}.tsv"
    meta = build_slice(src, a.n, out, a.workers)
    print(json.dumps({**meta, "slice": str(out)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
