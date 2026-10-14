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


from molseal import metrics
from molseal.engine import DEFAULT_FAMILIES, FEATMORGAN2, MORGAN2, MORGAN2_CHIRAL, FingerprintSpec


def test_family_spec_validation():
    FingerprintSpec("ok-name_1", 64, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("Bad Name", 2048, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("x", 100, lambda: None)
    with pytest.raises(ValueError):
        FingerprintSpec("x", 0, lambda: None)
    assert MORGAN2.nbytes == 256 and DEFAULT_FAMILIES == (MORGAN2,)


def test_duplicate_unknown_and_last_family_rules():
    with pytest.raises(ValueError):
        engine.StructureIndex((MORGAN2, MORGAN2))
    with pytest.raises(ValueError):
        engine.StructureIndex(())
    idx = engine.StructureIndex()
    with pytest.raises(ValueError, match="unknown fingerprint family"):
        idx.similar("CCO", family="nope")
    with pytest.raises(ValueError, match="unknown fingerprint family"):
        idx.fingerprints([uuid4()], "nope")
    with pytest.raises(ValueError):
        idx.drop_family("morgan2")                       # the last one
    idx.add_family(FEATMORGAN2)
    assert [f.name for f in idx.families] == ["morgan2", "featmorgan2"]
    idx.drop_family("morgan2")
    assert [f.name for f in idx.families] == ["featmorgan2"]
    with pytest.raises(ValueError):
        idx.add_family(FingerprintSpec("featmorgan2", 1024, FEATMORGAN2.generator))   # same name, other width
    idx.add_family(FEATMORGAN2)                           # same name and width: no-op


def test_generator_width_mismatch_is_refused():
    wide = FingerprintSpec("narrow", 128, MORGAN2.generator)   # says 128, makes 2048
    idx = engine.StructureIndex((MORGAN2, wide))
    with pytest.raises(ValueError, match="2048-bit"):
        idx.add_batch([(uuid4(), "CCO", 1)])


def test_add_family_later_equals_ingest_bit_for_bit(dataset):
    ids, smiles, _ = dataset
    items = [(i, s, 1) for i, s in zip(ids, smiles)]
    full = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    full.add_batch(items)
    full.add_batch([(ids[0], "c1ccccc1Cl", 2)])         # an edit: position 0 dies
    full.mark_dead(ids[1], 2)                            # a delete
    full.add_batch([(ids[2], "C1CC", 2)])                # an unindexable edit
    later = engine.StructureIndex((MORGAN2,))
    later.add_batch(items)
    later.add_batch([(ids[0], "c1ccccc1Cl", 2)])
    later.mark_dead(ids[1], 2)
    later.add_batch([(ids[2], "C1CC", 2)])
    later.add_family(FEATMORGAN2)
    later.add_family(MORGAN2_CHIRAL)
    for fam in ("morgan2", "featmorgan2", "morgan2-chiral"):
        f1, m1 = full.fingerprints(ids, fam)
        f2, m2 = later.fingerprints(ids, fam)
        assert f1 == f2 and ids[1] not in f1 and ids[2] not in f1 and ids[0] in f1
        assert np.array_equal(m1, m2) and m1.shape == (len(f1), 256)
    assert later._fams["featmorgan2"].fps.shape[0] == later._cap     # grown to the index capacity


def test_fingerprints_follow_the_order_given_and_skip_unknown(index, dataset):
    ids, smiles, mols = dataset
    want = [ids[3], uuid4(), ids[1]]
    found, mat = index.fingerprints(want)
    assert found == [ids[3], ids[1]]
    assert np.array_equal(mat, engine.morgan_matrix([mols[3], mols[1]]))
    found, mat = index.fingerprints([])
    assert found == [] and mat.shape == (0, 256)


def _bulk(gen, fn, query, mols):
    qfp = gen.GetFingerprint(Chem.MolFromSmiles(query))
    return fn(qfp, [gen.GetFingerprint(m) for m in mols])


@pytest.mark.parametrize("family,metric,bulk", [
    (FEATMORGAN2, metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
    (MORGAN2_CHIRAL, metrics.tanimoto, DataStructs.BulkTanimotoSimilarity),
    (MORGAN2, metrics.dice, DataStructs.BulkDiceSimilarity),
    (MORGAN2, metrics.cosine, DataStructs.BulkCosineSimilarity),
    (MORGAN2, metrics.tversky(1.0, 0.0), lambda q, fps: DataStructs.BulkTverskySimilarity(q, fps, 1.0, 0.0)),
])
def test_similar_with_family_and_metric_matches_rdkit(dataset, family, metric, bulk):
    ids, smiles, mols = dataset
    idx = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    gen = family.generator()
    for query in ("CCOc1ccccc1", "NC(=O)c1ccccc1", "C[C@H](N)C(=O)O"):
        expected = {i: s for i, s in zip(ids, _bulk(gen, bulk, query, mols)) if s >= 0.3}
        got = idx.similar(query, 0.3, family=family.name, metric=metric)
        assert {i for i, _ in got} == set(expected)
        assert all(abs(s - expected[i]) < 1e-9 for i, s in got)
        assert [s for _, s in got] == sorted((s for _, s in got), reverse=True)


def test_chiral_family_separates_enantiomers_and_the_default_does_not(dataset):
    ids, smiles, _ = dataset
    idx = engine.StructureIndex((MORGAN2, MORGAN2_CHIRAL))
    idx.add_batch([(i, s, 1) for i, s in zip(ids, smiles)])
    canon = lambda x: Chem.MolToSmiles(Chem.MolFromSmiles(x))
    l_ala = {i for i, s in zip(ids, smiles) if s == canon("C[C@H](N)C(=O)O")}
    d_ala = {i for i, s in zip(ids, smiles) if s == canon("C[C@@H](N)C(=O)O")}
    assert l_ala and d_ala
    exact_default = {i for i, s in idx.similar("C[C@H](N)C(=O)O", 0.999)}
    exact_chiral = {i for i, s in idx.similar("C[C@H](N)C(=O)O", 0.999, family="morgan2-chiral")}
    assert d_ala <= exact_default and l_ala <= exact_chiral and not (d_ala & exact_chiral)


def test_parts_carry_every_family_and_missing_ones_are_recomputed(dataset):
    ids, smiles, _ = dataset
    items = [(i, s, 1) for i, s in zip(ids, smiles)]
    two = engine.StructureIndex((MORGAN2, FEATMORGAN2))
    two.add_batch(items)
    two.mark_dead(ids[0], 2)
    parts = two.to_parts()
    assert set(parts) == {"meta", "lib", "ids", "live", "rv", "fp:morgan2", "fp:featmorgan2"}
    same = engine.StructureIndex.from_parts(parts, (MORGAN2, FEATMORGAN2))
    fewer = engine.StructureIndex.from_parts(parts, (MORGAN2,))
    more = engine.StructureIndex.from_parts(parts, (FEATMORGAN2, MORGAN2_CHIRAL))
    assert [f.name for f in fewer.families] == ["morgan2"]
    assert [f.name for f in more.families] == ["featmorgan2", "morgan2-chiral"]
    fresh = engine.StructureIndex((MORGAN2, FEATMORGAN2, MORGAN2_CHIRAL))
    fresh.add_batch(items)
    fresh.mark_dead(ids[0], 2)
    for idx, fam in ((same, "featmorgan2"), (more, "featmorgan2"), (more, "morgan2-chiral"), (fewer, "morgan2")):
        f1, m1 = idx.fingerprints(ids, fam)
        f2, m2 = fresh.fingerprints(ids, fam)
        assert f1 == f2 and np.array_equal(m1, m2)
    assert {i for i, _ in more.similar(smiles[7], 0.999, family="morgan2-chiral")} == {ids[7]}
    assert len(same) == len(ids) - 1


def brute_force_chiral(dataset, query):
    ids, _, mols = dataset
    q = Chem.MolFromSmiles(query)
    return {i for i, m in zip(ids, mols) if m.HasSubstructMatch(q, useChirality=True)}


def test_generalized_finds_the_tautomer_and_plain_does_not():
    idx = engine.StructureIndex()
    keto, enol, other = uuid4(), uuid4(), uuid4()
    idx.add_batch([(keto, "O=c1cccc[nH]1", 1), (enol, "Oc1ccccn1", 1), (other, "c1ccccc1", 1)])
    assert set(idx.substructure("Oc1ccccn1")) == {enol}
    assert set(idx.substructure("Oc1ccccn1", generalized=True)) == {keto, enol}
    assert idx.count_substructure("Oc1ccccn1", generalized=True) == 2
    assert idx.count_substructure("Oc1ccccn1") == 1
    assert idx._lib.GetFpHolder().__class__.__name__ == "TautomerPatternHolder"


def test_use_chirality_matches_only_the_same_defined_stereo(index, dataset):
    ids, smiles, _ = dataset
    q = "C[C@H](N)C(=O)O"
    expected_on = brute_force_chiral(dataset, q)
    assert set(index.substructure(q, use_chirality=True)) == expected_on
    assert index.count_substructure(q, use_chirality=True) == len(expected_on)
    assert expected_on < brute_force(dataset, q, "smiles")                 # strict subset: the enantiomer and the flat one drop out
    flat = "CC(N)C(=O)O"
    assert set(index.substructure(flat, use_chirality=True)) == brute_force(dataset, flat, "smiles")   # undefined query matches all


def test_molblock_query_with_explicit_hydrogens_and_kekule_ring(index):
    block = Chem.MolToMolBlock(Chem.AddHs(Chem.MolFromSmiles("Oc1ccccc1")), kekulize=True)
    assert "H " in block and "M  END" in block
    assert set(index.substructure(block, kind="molblock")) == set(index.substructure("Oc1ccccc1"))
    assert index.count_substructure(block, kind="molblock") == index.count_substructure("Oc1ccccc1")
    with pytest.raises(engine.StructureError):
        index.substructure("not a molblock", kind="molblock")
    with pytest.raises(engine.StructureError):
        index.substructure("CCO", kind="inchi")


def test_generalized_with_smarts_or_molblock_answers_or_raises_cleanly(index):
    for query, kind in (("[#6]-[#7]-[#6](=O)", "smarts"), (Chem.MolToMolBlock(Chem.MolFromSmiles("NC(=O)C")), "molblock")):
        try:
            hits = index.substructure(query, kind=kind, generalized=True)
        except engine.StructureError:
            continue
        assert set(hits) >= set(index.substructure(query, kind=kind))


def test_capped_search_is_deterministic_on_one_thread(index):
    runs = {tuple(index.substructure("c1ccccc1", max_results=10, num_threads=1)) for _ in range(5)}
    assert len(runs) == 1 and len(next(iter(runs))) == 10
