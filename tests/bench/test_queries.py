import json
import random

from rdkit import Chem

from bench import queries as q
from tests.mols import molecules


def test_functional_groups_all_parse_and_include_ring_queries():
    names = {n for n, _ in q.FUNCTIONAL_GROUPS}
    assert len(q.FUNCTIONAL_GROUPS) >= 20
    assert {"ring-atom", "five-ring", "ring-junction"} <= names
    for _, smarts in q.FUNCTIONAL_GROUPS:
        assert Chem.MolFromSmarts(smarts) is not None


def test_fragments_are_sized_deduplicated_and_free_of_dummies():
    frags = q.fragments_from(molecules(), random.Random(1), n=20)
    assert 1 <= len(frags) <= 20 and len(set(frags)) == len(frags)
    for f in frags:
        m = Chem.MolFromSmiles(f)
        assert m is not None and "*" not in f
        assert 5 <= m.GetNumHeavyAtoms() <= 20


def test_build_queries_is_deterministic(tmp_path):
    slice_path = tmp_path / "slice_438.tsv"
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tK{i}\n" for i, s in enumerate(molecules())))
    a = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    b = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    assert a == b
    assert len(a["similarity"]) == 5 and all(s["source_id"] for s in a["similarity"])
    kinds = {s["kind"] for s in a["substructure"]}
    assert kinds == {"smiles", "smarts"}
    assert json.dumps(a)   # serializable
