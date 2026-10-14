"""A deterministic, offline set of a few hundred valid molecules for oracle tests."""

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

TEMPLATES = [
    "{R}c1ccccc1", "{R}C1CCCCC1", "{R}c1ccncc1", "{R}c1ccc(cc1){S}", "{R}C(=O)N{S}", "{R}OC(=O){S}",
    "{R}NC(=O)c1ccccc1", "{R}S(=O)(=O)N", "{R}C#N", "{R}C1CC1", "{R}c1ccc2[nH]ccc2c1", "{R}C1CCNCC1",
    "{R}c1cnc[nH]1", "{R}N1CCOCC1", "{R}c1ccc2ccccc2c1", "{R}C(=O)O", "{R}C(=O){S}", "{R}N{S}",
]
R_GROUPS = [
    "C", "CC", "CCC", "CC(C)C", "CCO", "CCN", "CCCl", "CF", "C(F)(F)F", "CO", "CN(C)C", "Cc1ccccc1",
    "CCC(=O)O", "CCS", "c1ccccc1", "C1CCCC1", "CC=C", "CC#C", "CCBr", "C(=O)C",
]
STEREO = ["C[C@H](N)C(=O)O", "C[C@@H](N)C(=O)O", "CC(N)C(=O)O", "C/C=C/C(=O)O", "C/C=C\\C(=O)O", "CC=CC(=O)O"]


def molecules() -> list[str]:
    seen: dict[str, None] = {}
    for t in TEMPLATES:
        for r in R_GROUPS:
            for s in ("C", "CCO", "c1ccccc1"):
                mol = Chem.MolFromSmiles(t.format(R=r, S=s))
                if mol is not None:
                    seen[Chem.MolToSmiles(mol)] = None
    for s in STEREO:
        seen[Chem.MolToSmiles(Chem.MolFromSmiles(s))] = None
    return list(seen)
