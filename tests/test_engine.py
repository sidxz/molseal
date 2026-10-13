from uuid import uuid4

import pytest
from rdkit import Chem

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
    import numpy as np
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
