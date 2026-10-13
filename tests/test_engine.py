from uuid import uuid4

import numpy as np
import pytest
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

from molseal import engine
from tests.mols import molecules

QUERIES = [
    ("c1ccccc1", "smiles"), ("CCO", "smiles"), ("C(=O)N", "smiles"), ("C#N", "smiles"), ("C1CC1", "smiles"),
    ("[R]", "smarts"), ("[r5]", "smarts"), ("[x3]", "smarts"), ("[Cl,Br]", "smarts"), ("[#7;!$(N-C=O)]", "smarts"),
    ("c1ccc2[nH]ccc2c1", "smiles"), ("C[C@H](N)C(=O)O", "smiles"), ("[CX4][OX2H]", "smarts"), ("FC(F)F", "smiles"),
]


@pytest.fixture(scope="module")
def dataset():
    smiles = molecules()
    ids = [uuid4() for _ in smiles]
    mols = [Chem.MolFromSmiles(s) for s in smiles]
    return ids, smiles, mols


@pytest.fixture(scope="module")
def index(dataset):
    ids, smiles, _ = dataset
    idx = engine.StructureIndex()
    added = idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    assert added == len(ids)
    return idx


def brute_force(dataset, query, kind):
    ids, _, mols = dataset
    q = Chem.MolFromSmarts(query) if kind == "smarts" else Chem.MolFromSmiles(query)
    return {i for i, m in zip(ids, mols) if m.HasSubstructMatch(q, useChirality=False)}


def test_canonicalize_round_trips_and_rejects_garbage():
    assert engine.canonicalize("OCC") == "CCO"
    assert engine.canonicalize("C1=CC=CC=C1") == "c1ccccc1"
    with pytest.raises(engine.StructureError):
        engine.canonicalize("not a molecule")


def test_morgan_matrix_shape_and_known_bit_count():
    m = engine.morgan_matrix([Chem.MolFromSmiles("CCO")])
    assert m.shape == (1, engine.FP_BYTES) and m.dtype.name == "uint8"
    assert 1 <= int(np.bitwise_count(m).sum()) <= 20


@pytest.mark.parametrize("query,kind", QUERIES)
def test_substructure_matches_brute_force_with_chirality_off(index, dataset, query, kind):
    expected = brute_force(dataset, query, kind)
    got = index.substructure(query, kind=kind)
    assert set(got) == expected
    assert len(got) == len(set(got))
    assert index.count_substructure(query, kind=kind) == len(expected)


def test_chiral_query_matches_both_enantiomers_and_the_flat_one(index, dataset):
    expected = brute_force(dataset, "C[C@H](N)C(=O)O", "smiles")
    assert len(expected) >= 3
    assert set(index.substructure("C[C@H](N)C(=O)O")) == expected


def test_max_results_caps_and_defaults_to_uncapped(index, dataset):
    total = index.count_substructure("c1ccccc1")
    assert total > 10
    assert len(index.substructure("c1ccccc1")) == total
    assert len(index.substructure("c1ccccc1", max_results=10)) == 10


def test_empty_index_and_no_match_return_empty():
    idx = engine.StructureIndex()
    assert idx.substructure("CCO") == []
    assert idx.count_substructure("CCO") == 0
    idx.add_batch([(uuid4(), "CCO", 1)])
    assert idx.substructure("c1ccccc1") == []


def test_invalid_query_raises():
    idx = engine.StructureIndex()
    idx.add_batch([(uuid4(), "CCO", 1)])
    with pytest.raises(engine.StructureError):
        idx.substructure("C(", kind="smiles")
    with pytest.raises(engine.StructureError):
        idx.substructure("[", kind="smarts")


def test_add_batch_skips_unparseable_and_already_seen():
    idx = engine.StructureIndex()
    a = uuid4()
    assert idx.add_batch([(a, "CCO", 1), (uuid4(), "garbage(", 1)]) == 1
    assert idx.add_batch([(a, "CCO", 1)]) == 0
    assert idx.has(a, 1) and not idx.has(a, 2)
    assert len(idx) == 1 and idx.positions == 1


def brute_similar(dataset, query, threshold):
    ids, _, mols = dataset
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    qfp = gen.GetFingerprint(Chem.MolFromSmiles(query))
    sims = DataStructs.BulkTanimotoSimilarity(qfp, [gen.GetFingerprint(m) for m in mols])
    return {i: s for i, s in zip(ids, sims) if s >= threshold}


@pytest.mark.parametrize("query", ["CCOc1ccccc1", "CC(C)Cc1ccncc1", "NC(=O)c1ccccc1", "CCCl"])
def test_similar_matches_bulk_tanimoto(index, dataset, query):
    expected = brute_similar(dataset, query, 0.4)
    got = index.similar(query, threshold=0.4)
    assert {i for i, _ in got} == set(expected)
    for i, s in got:
        assert abs(s - expected[i]) < 1e-9
    scores = [s for _, s in got]
    assert scores == sorted(scores, reverse=True)


def test_similar_top_k_and_self_hit(index, dataset):
    ids, smiles, _ = dataset
    got = index.similar(smiles[5], threshold=0.0, top_k=3)
    assert len(got) == 3
    assert got[0][0] == ids[5] and abs(got[0][1] - 1.0) < 1e-9


def test_similar_on_empty_index_and_bad_query():
    idx = engine.StructureIndex()
    assert idx.similar("CCO") == []
    idx.add_batch([(uuid4(), "CCO", 1)])
    with pytest.raises(engine.StructureError):
        idx.similar("C(")


def test_edit_hides_the_old_structure_and_never_duplicates_the_id():
    idx = engine.StructureIndex()
    x, y = uuid4(), uuid4()
    idx.add_batch([(x, "CCO", 1), (y, "CCN", 1)])
    assert set(idx.substructure("CCO")) == {x}
    idx.add_batch([(x, "c1ccccc1", 2)])
    assert idx.substructure("CCO") == []            # old structure of x is gone
    assert idx.substructure("c1ccccc1") == [x]      # new one is there, once
    assert len(idx) == 2 and idx.positions == 3
    assert {i for i, _ in idx.similar("c1ccccc1", threshold=0.99)} == {x}
    assert {i for i, _ in idx.similar("CCO", threshold=0.99)} == set()


def test_delete_removes_from_both_searches_and_caps_stay_honest():
    idx = engine.StructureIndex()
    ids = [uuid4() for _ in range(5)]
    idx.add_batch([(i, "CCO", 1) for i in ids])
    assert idx.mark_dead(ids[0], 2) and idx.mark_dead(ids[1], 2)
    assert not idx.mark_dead(ids[1], 2)             # already at this version
    assert len(idx) == 3
    assert set(idx.substructure("CCO", max_results=5)) == set(ids[2:])
    assert idx.count_substructure("CCO") == 3
    assert {i for i, _ in idx.similar("CCO", threshold=0.99)} == set(ids[2:])


def test_append_after_a_delete_is_visible():
    idx = engine.StructureIndex()
    a, b = uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1)])
    idx.mark_dead(a, 2)
    idx.add_batch([(b, "CCO", 1)])
    assert idx.substructure("CCO") == [b]


def test_parts_round_trip_preserves_results_and_dead_mask():
    idx = engine.StructureIndex()
    a, b, c = uuid4(), uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "c1ccccc1", 1), (c, "CCN", 1)])
    idx.mark_dead(c, 2)
    idx.add_batch([(a, "CCCl", 2)])
    idx.last_seq = 42
    parts = idx.to_parts()
    back = engine.StructureIndex.from_parts(parts)
    assert back.last_seq == 42 and len(back) == 2 and back.positions == 4
    assert back.substructure("CCCl") == [a] and back.substructure("CCO") == [] and back.substructure("CCN") == []
    assert back.has(a, 2) and back.has(c, 2) and not back.has(c, 3)
    assert {i for i, _ in back.similar("c1ccccc1", threshold=0.99)} == {b}
    assert back.add_batch([(uuid4(), "CCO", 1)]) == 1


def test_append_after_an_applied_order_is_visible_and_order_is_lazy(monkeypatch):
    idx = engine.StructureIndex()
    a, b, c = uuid4(), uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "CCO", 1)])
    idx.mark_dead(a, 2)
    assert idx.substructure("CCO") == [b]           # order applied here, once
    calls = []
    real = idx._lib.SetSearchOrder
    monkeypatch.setattr(idx, "_lib", type("L", (), {"__getattr__": lambda s, n: getattr(real.__self__, n),
                                                    "SetSearchOrder": lambda s, o: calls.append(o) or real(o)})())
    for i in range(3):
        idx.add_batch([(uuid4(), "CCN", 1)])
    idx.mark_dead(b, 2)
    assert calls == []                              # nothing applied until a search
    idx.add_batch([(c, "CCO", 1)])
    assert idx.substructure("CCO") == [c] and idx.count_substructure("CCO") == 1
    assert len(calls) == 1


def test_unparseable_rows_are_recorded_and_round_trip():
    idx = engine.StructureIndex()
    a, b = uuid4(), uuid4()
    idx.add_batch([(a, "CCO", 1), (b, "CCN", 1)])
    assert idx.add_batch([(a, "C1CC", 2)]) == 0
    assert idx.unindexed == {a} and idx.has(a, 2) and len(idx) == 1
    assert idx.substructure("CCO") == []            # the old version is not served
    back = engine.StructureIndex.from_parts(idx.to_parts())
    assert back.unindexed == {a}
    back.add_batch([(a, "CCCl", 3)])
    assert back.unindexed == set() and back.substructure("CCCl") == [a]
    back.mark_dead(b, 2)
    idx.mark_dead(a, 3)
    assert idx.unindexed == set()


def test_canonicalize_rejects_output_that_does_not_re_parse(monkeypatch):
    real = engine.Chem.MolFromSmiles
    monkeypatch.setattr(engine.Chem, "MolFromSmiles", lambda s: real(s) if s == "OCC" else None)
    with pytest.raises(engine.StructureError, match="re-parse"):
        engine.canonicalize("OCC")
