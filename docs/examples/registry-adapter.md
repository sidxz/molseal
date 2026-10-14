# Example: a compound registry on molseal

Generic sketch of how a registry application maps its own vocabulary onto molseal's primitives. The
library knows none of these names; they live in the application.

```python
from molseal import metrics
from molseal.engine import FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL
from molseal.vault import Vault

vault = Vault(store, key_provider, workspace_id, snapshot_dir=snapshots,
              families=(MORGAN2, MORGAN2_CHIRAL, FEATMORGAN2))

# Similarity presets are (family, metric, threshold) triples. Thresholds never transfer across triples.
PRESETS = {
    "similar":            ("morgan2",      metrics.tanimoto,      0.70),
    "scaffold-hop":       ("featmorgan2",  metrics.tanimoto,      0.55),
    "fragment-in-target": ("morgan2",      metrics.tversky(1, 0), 0.70),
    "same-stereo":        ("morgan2-chiral", metrics.tanimoto,    0.85),
}

def similar(mode: str, smiles: str, top_k: int | None = None):
    family, metric, threshold = PRESETS[mode]
    return vault.similar(standardize(smiles), threshold, top_k, family=family, metric=metric)
```

Standardize the query exactly as you standardize structures at registration (`standardize` is your
pipeline: salt stripping, charge neutralization, whatever you chose). The library stores what it is
given.

## Substructure from a sketcher

```python
def substructure(molblock: str, *, tautomers: bool = False, stereo: bool = False, limit: int | None = 1000):
    return vault.substructure(molblock, kind="molblock", max_results=limit,
                              generalized=tautomers, use_chirality=stereo)
```

A molblock query keeps a sketcher's drawing intact: explicit hydrogens are dropped and Kekulé rings are
aromatized on parse. A SMARTS query is taken literally.

## Registration and dedup through exact keys

```python
def register(molecule_id, smiles: str, inchikey: str, parent_inchikey: str):
    vault.put(molecule_id, smiles, {"inchikey": inchikey, "parent": parent_inchikey})

def already_registered(inchikey: str) -> list:
    return vault.find_exact("inchikey", inchikey)
```

Exact keys are HMACs keyed per tenant; the database never sees the InChIKey. Uniqueness is the
application's rule: keep it in your own table if two writers may race.

## Analytics on bulk reads

```python
from rdkit import DataStructs
from rdkit.ML.Cluster import Butina
import numpy as np

ids, rows = vault.fingerprints(candidate_ids, family="morgan2")
bvs = []
for row in rows:
    bv = DataStructs.ExplicitBitVect(2048)
    bv.SetBitsFromList(np.flatnonzero(np.unpackbits(row)).tolist())
    bvs.append(bv)
dists = [1 - s for i in range(1, len(bvs)) for s in DataStructs.BulkTanimotoSimilarity(bvs[i], bvs[:i])]
clusters = Butina.ClusterData(dists, len(bvs), 0.4, isDistData=True)

payloads = vault.get_many(ids)        # decrypted structures for scaffold or MCS work in your own code
```

Treat `rows` and `payloads` as confidential: they leave the vault only for this computation.
