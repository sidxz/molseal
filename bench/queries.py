"""Seeded query set: BRICS fragments of real molecules at varied sizes, functional-group and ring
SMARTS, and similarity queries drawn from the slice. Published with the results."""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from rdkit import Chem, RDLogger
from rdkit.Chem import BRICS

from bench.load_molseal import row_id

RDLogger.DisableLog("rdApp.*")

FUNCTIONAL_GROUPS: list[tuple[str, str]] = [
    ("amide", "C(=O)N"), ("carboxylic-acid", "C(=O)[OH]"), ("ester", "C(=O)O[#6]"), ("sulfonamide", "S(=O)(=O)N"),
    ("nitrile", "C#N"), ("nitro", "[N+](=O)[O-]"), ("halogen", "[Cl,Br,I]"), ("trifluoromethyl", "C(F)(F)F"),
    ("phenol", "c[OH]"), ("aniline", "c[NH2]"), ("pyridine", "n1ccccc1"), ("piperidine", "C1CCNCC1"),
    ("piperazine", "N1CCNCC1"), ("morpholine", "C1COCCN1"), ("indole", "c1ccc2[nH]ccc2c1"), ("urea", "NC(=O)N"),
    ("ring-atom", "[R]"), ("five-ring", "[r5]"), ("ring-junction", "[x3]"), ("ring-bond-CN", "[#6]@[#7]"),
    ("non-amide-N", "[#7;!$(N-C=O)]"), ("alcohol", "[CX4][OX2H]"),
]
_DUMMY = Chem.MolFromSmarts("[#0]")


def fragments_from(smiles_list: list[str], rng: random.Random, n: int = 50, min_atoms: int = 5, max_atoms: int = 20) -> list[str]:
    seen: dict[str, int] = {}
    for smi in smiles_list:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        for frag in BRICS.BRICSDecompose(mol):
            fm = Chem.MolFromSmiles(frag)
            if fm is None:
                continue
            fm = Chem.DeleteSubstructs(fm, _DUMMY)
            try:
                Chem.SanitizeMol(fm)
            except Exception:
                continue
            if fm.GetNumAtoms() == 0 or len(Chem.GetMolFrags(fm)) != 1:
                continue
            canon = Chem.MolToSmiles(fm)
            atoms = fm.GetNumHeavyAtoms()
            if min_atoms <= atoms <= max_atoms:
                seen[canon] = atoms
    pool = sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))
    if len(pool) <= n:
        return [s for s, _ in pool]
    step = len(pool) / n                       # evenly spaced across sizes, seeded jitter inside each step
    picks = [pool[min(len(pool) - 1, int(i * step + rng.random() * step))][0] for i in range(n)]
    return list(dict.fromkeys(picks))


def build_queries(slice_path: Path, seed: int = 20261013, n_frag: int = 50, n_sim: int = 50, sample: int = 2000) -> dict:
    rng = random.Random(seed)
    rows = [line.rstrip("\n").split("\t") for line in open(slice_path)]
    picked = rng.sample(rows, min(sample, len(rows)))
    frags = fragments_from([r[1] for r in picked], rng, n=n_frag)
    sub = [{"id": f"frag-{i:03d}", "kind": "smiles", "query": f} for i, f in enumerate(frags)]
    sub += [{"id": f"fg-{name}", "kind": "smarts", "query": smarts} for name, smarts in FUNCTIONAL_GROUPS]
    sims = rng.sample(rows, min(n_sim, len(rows)))
    sim = [{"id": f"sim-{i:03d}", "query": r[1], "source_id": str(row_id(r[0]))} for i, r in enumerate(sims)]
    return {"seed": seed, "slice": str(slice_path), "substructure": sub, "similarity": sim}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="build the seeded query set for a slice")
    p.add_argument("--slice", required=True)
    p.add_argument("--seed", type=int, default=20261013)
    a = p.parse_args(argv)
    qs = build_queries(Path(a.slice), a.seed)
    n = Path(a.slice).stem.split("_")[-1]
    out = Path(a.slice).parent / f"queries_{n}.json"
    out.write_text(json.dumps(qs, indent=1))
    print(f"{len(qs['substructure'])} substructure, {len(qs['similarity'])} similarity -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
