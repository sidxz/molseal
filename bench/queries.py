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
def strip_dummies(frag: str) -> Chem.Mol | None:
    """A BRICS fragment without its [n*] dummies. The former attachment atoms get their hydrogens back
    instead of becoming radicals: a radical query only matches radicals, so it would hit nothing."""
    m = Chem.RWMol(Chem.MolFromSmiles(frag, sanitize=False))
    dummies = [a.GetIdx() for a in m.GetAtoms() if a.GetAtomicNum() == 0]
    attach = {n.GetIdx() for d in dummies for n in m.GetAtomWithIdx(d).GetNeighbors()}
    for a in attach:
        m.GetAtomWithIdx(a).SetNumRadicalElectrons(0)
        m.GetAtomWithIdx(a).SetNoImplicit(False)
    for d in sorted(dummies, reverse=True):
        m.RemoveAtom(d)
    fm = m.GetMol()
    try:
        Chem.SanitizeMol(fm)
    except Exception:
        return None
    return fm if fm.GetNumAtoms() and len(Chem.GetMolFrags(fm)) == 1 and not any(
        a.GetNumRadicalElectrons() for a in fm.GetAtoms()) else None


def fragments_from(smiles_list: list[str], rng: random.Random, n: int = 50, min_atoms: int = 5, max_atoms: int = 20) -> list[str]:
    """Fragments of the given molecules, each one checked to match the molecule it was cut from."""
    seen: dict[str, int] = {}
    for smi in smiles_list:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            continue
        for frag in BRICS.BRICSDecompose(mol):
            fm = strip_dummies(frag)
            if fm is None:
                continue
            canon = Chem.MolToSmiles(fm)
            atoms = fm.GetNumHeavyAtoms()
            if min_atoms <= atoms <= max_atoms and mol.HasSubstructMatch(Chem.MolFromSmiles(canon)):
                seen[canon] = atoms
    pool = sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))
    if len(pool) <= n:
        return [s for s, _ in pool]
    step = len(pool) / n                       # evenly spaced across sizes, seeded jitter inside each step
    picks = [pool[min(len(pool) - 1, int(i * step + rng.random() * step))][0] for i in range(n)]
    return list(dict.fromkeys(picks))


def mirror(smiles: str) -> str | None:
    """The mirror image: every @@ becomes @ and every @ becomes @@. None if it does not re-parse."""
    flipped = smiles.replace("@@", "\0").replace("@", "@@").replace("\0", "@")
    m = Chem.MolFromSmiles(flipped)
    return None if m is None else Chem.MolToSmiles(m)


def chiral_queries(smiles_list: list[str], rng: random.Random, n: int = 20, max_atoms: int = 30) -> list[str]:
    """n seeded molecules with at least one assigned tetrahedral stereocentre and at most max_atoms heavy
    atoms, each followed by its mirror image when that differs. Canonical SMILES."""
    pool: set[str] = set()
    for smi in smiles_list:
        mol = Chem.MolFromSmiles(smi)
        if mol is None or mol.GetNumHeavyAtoms() > max_atoms:
            continue
        if Chem.FindMolChiralCenters(mol, includeUnassigned=False, useLegacyImplementation=False):
            pool.add(Chem.MolToSmiles(mol))
    out: list[str] = []
    for s in rng.sample(sorted(pool), min(n, len(pool))):
        out.append(s)
        m = mirror(s)
        if m is not None and m != s:
            out.append(m)
    return out

def build_queries(slice_path: Path, seed: int = 20261013, n_frag: int = 50, n_sim: int = 50, sample: int = 2000,
                 n_chiral: int = 20) -> dict:
    rng = random.Random(seed)
    with open(slice_path) as f:
        rows = [line.rstrip("\n").split("\t") for line in f]
    picked = rng.sample(rows, min(sample, len(rows)))
    frags = fragments_from([r[1] for r in picked], rng, n=n_frag)
    sub = [{"id": f"frag-{i:03d}", "kind": "smiles", "query": f} for i, f in enumerate(frags)]
    sub += [{"id": f"fg-{name}", "kind": "smarts", "query": smarts} for name, smarts in FUNCTIONAL_GROUPS]
    sims = rng.sample(rows, min(n_sim, len(rows)))
    sim = [{"id": f"sim-{i:03d}", "query": r[1], "source_id": str(row_id(r[0]))} for i, r in enumerate(sims)]
    chiral = [{"id": f"chi-{i:03d}", "query": s} for i, s in enumerate(chiral_queries([r[1] for r in picked], rng, n=n_chiral))]
    return {"seed": seed, "slice": str(slice_path), "substructure": sub, "similarity": sim, "chiral": chiral}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="build the seeded query set for a slice")
    p.add_argument("--slice", required=True)
    p.add_argument("--seed", type=int, default=20261013)
    a = p.parse_args(argv)
    qs = build_queries(Path(a.slice), a.seed)
    n = Path(a.slice).stem.split("_")[-1]
    out = Path(a.slice).parent / f"queries_{n}.json"
    out.write_text(json.dumps(qs, indent=1))
    print(f"{len(qs['substructure'])} substructure, {len(qs['similarity'])} similarity, {len(qs['chiral'])} chiral -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
