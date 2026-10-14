from uuid import uuid4

from bench import parity


def test_compare_sets_reports_differences_with_examples():
    a, b, c = uuid4(), uuid4(), uuid4()
    r = parity.compare_sets({a, b}, {b, c})
    assert r["equal"] is False and r["only_a"] == 1 and r["only_b"] == 1
    assert r["examples_only_a"] == [str(a)] and r["examples_only_b"] == [str(c)]
    assert parity.compare_sets({a}, {a})["equal"] is True


def test_compare_scores_uses_tolerance():
    a, b = uuid4(), uuid4()
    assert parity.compare_scores({a: 0.5, b: 0.9}, {a: 0.5000000001, b: 0.9})["equal"] is True
    r = parity.compare_scores({a: 0.5}, {a: 0.6})
    assert r["equal"] is False and r["max_abs_diff"] > 0.09


def test_cartridge_query_sql_picks_the_constructor():
    assert "mol_from_smiles" in parity.cartridge_query_sql("smiles")
    assert "qmol_from_smarts" in parity.cartridge_query_sql("smarts")


def test_check_substructure_counts_lists_and_rejected_rows():
    a, b, r = uuid4(), uuid4(), uuid4()
    g2, g3 = parity.check_substructure("q", [a, b, r], 3, 2, {r}, lambda: {a, b})
    assert g2 == {"id": "q", "molseal": 2, "molseal_count": 2, "cartridge": 2, "ok": True} and g3["ok"]
    g2, _ = parity.check_substructure("q", [a, b], 3, 2, set(), lambda: {a, b})        # count path disagrees
    assert not g2["ok"]
    g2, g3 = parity.check_substructure("q", [a, a], 2, 2, set(), lambda: {a})          # duplicate hit shows
    assert g2["ok"] and not g3["ok"]
    g2, g3 = parity.check_substructure("q", [a], 1, 1, set(), lambda: {b})
    assert g2["ok"] and not g3["ok"] and g3["only_a"] == 1


def test_check_substructure_names_the_id_set_skip():
    _, g3 = parity.check_substructure("q", [], 0, 20_000, set(), lambda: 1 / 0, id_set_limit=10_000)
    assert g3 == {"id": "q", "skipped": "count > 10000", "ok": True}


def test_check_similarity_drops_source_and_rejected():
    s, a, r = uuid4(), uuid4(), uuid4()
    rec = parity.check_similarity("q", [(s, 1.0), (a, 0.8), (r, 0.9)], {s: 1.0, a: 0.8}, s, {r})
    assert rec["ok"] and rec["molseal"] == rec["cartridge"] == 1
    assert not parity.check_similarity("q", [(a, 0.8)], {a: 0.7}, s, set())["ok"]


def test_overall_ok_needs_every_gate():
    good = {"gate1": {"ok": True}, "gate2": [{"ok": True}], "gate3": [{"ok": True}], "gate4": [{"ok": True}]}
    assert parity.overall_ok(good)
    for g in ("gate2", "gate3", "gate4"):
        assert not parity.overall_ok({**good, g: [{"ok": True}, {"ok": False}]})
    assert not parity.overall_ok({**good, "gate1": {"ok": False}})


import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

from molseal import metrics


def test_check_chiral_requires_equal_on_sets_and_supersets_on_both_sides():
    a, b, r = uuid4(), uuid4(), uuid4()
    ok = parity.check_chiral("q", [a, r], {a}, [a, b, r], {a, b}, {r})
    assert ok["ok"] and ok["equal"] and ok["superset_molseal"] and ok["superset_cartridge"]
    assert not parity.check_chiral("q", [a], {b}, [a, b], {a, b}, set())["ok"]           # on sets differ
    assert not parity.check_chiral("q", [a], {a}, [b], {a, b}, set())["ok"]              # molseal off is not a superset
    assert not parity.check_chiral("q", [a], {a}, [a, b], {b}, set())["ok"]              # cartridge off is not a superset


def test_check_rows_counts_mismatches_and_missing_ids():
    a, b = uuid4(), uuid4()
    ref = {a: np.array([1, 2], dtype=np.uint8), b: np.array([3, 4], dtype=np.uint8)}
    assert parity.check_rows([a, b], np.array([[1, 2], [3, 4]], dtype=np.uint8), ref)["ok"]
    bad = parity.check_rows([a, b], np.array([[1, 2], [9, 4]], dtype=np.uint8), ref)
    assert not bad["ok"] and bad["mismatches"] == 1
    missing = parity.check_rows([a], np.array([[1, 2]], dtype=np.uint8), ref)
    assert not missing["ok"] and missing["missing"] == 1


def test_gate_six_would_catch_a_swapped_tversky_pair():
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    query = gen.GetFingerprint(Chem.MolFromSmiles("c1ccccc1"))
    targets = [gen.GetFingerprint(Chem.MolFromSmiles(s)) for s in ("c1ccccc1CCN", "CCOc1ccccc1C(=O)O", "CCCC")]
    ids = [uuid4() for _ in targets]
    qbits = set(query.GetOnBits())
    c = np.array([len(qbits & set(t.GetOnBits())) for t in targets])
    b = np.array([t.GetNumOnBits() for t in targets])
    ours = dict(zip(ids, metrics.tversky(1, 0)(c, query.GetNumOnBits(), b)))
    right = {i: DataStructs.TverskySimilarity(query, t, 1.0, 0.0) for i, t in zip(ids, targets)}   # query first
    swapped = {i: DataStructs.TverskySimilarity(t, query, 1.0, 0.0) for i, t in zip(ids, targets)}
    assert parity.compare_scores(ours, right)["equal"]
    assert not parity.compare_scores(ours, swapped)["equal"]
    assert parity.SQL_TVERSKY.count("tversky_sml(morganbv_fp(mol_from_smiles(") == 2       # the query is the first argument


def test_overall_ok_covers_gates_five_to_ten():
    good = {"gate1": {"ok": True}, **{f"gate{n}": [{"ok": True}] for n in range(2, 11)}}
    assert parity.overall_ok(good)
    for n in range(5, 11):
        assert not parity.overall_ok({**good, f"gate{n}": [{"ok": True}, {"ok": False}]})
