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


def test_fragments_carry_no_radicals_and_match_their_parent():
    parents = [Chem.MolFromSmiles(s) for s in molecules()]
    frags = q.fragments_from(molecules(), random.Random(1), n=40)
    assert frags
    for f in frags:
        m = Chem.MolFromSmiles(f)
        assert not any(a.GetNumRadicalElectrons() for a in m.GetAtoms()), f
        assert any(p.HasSubstructMatch(m) for p in parents), f


def test_strip_dummies_gives_hydrogens_back_instead_of_radicals():
    assert Chem.MolToSmiles(q.strip_dummies("[8*]C(C=O)CC")) == "CCCC=O"


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


def test_chiral_queries_have_assigned_centres_and_mirror_images():
    out = q.chiral_queries(molecules(), random.Random(3), n=20)
    assert len(out) >= 2
    for s in out:
        m = Chem.MolFromSmiles(s)
        assert m is not None and m.GetNumHeavyAtoms() <= 30
        assert Chem.FindMolChiralCenters(m, includeUnassigned=False, useLegacyImplementation=False)
    l_ala = Chem.MolToSmiles(Chem.MolFromSmiles("C[C@H](N)C(=O)O"))
    d_ala = Chem.MolToSmiles(Chem.MolFromSmiles("C[C@@H](N)C(=O)O"))
    assert q.mirror(l_ala) == d_ala and q.mirror(d_ala) == l_ala
    assert q.mirror("not a molecule") is None
    assert out == q.chiral_queries(molecules(), random.Random(3), n=20)        # seeded


def test_chiral_queries_are_drawn_after_the_rest_so_old_sections_do_not_move(tmp_path, monkeypatch):
    slice_path = tmp_path / "slice_438.tsv"
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tK{i}\n" for i, s in enumerate(molecules())))
    with_chiral = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    monkeypatch.setattr(q, "chiral_queries", lambda *a, **k: [])
    without = q.build_queries(slice_path, seed=7, n_frag=10, n_sim=5, sample=100)
    assert with_chiral["substructure"] == without["substructure"]
    assert with_chiral["similarity"] == without["similarity"]
    assert with_chiral["chiral"] and without["chiral"] == []
    assert all(c["id"].startswith("chi-") and c["query"] for c in with_chiral["chiral"])
