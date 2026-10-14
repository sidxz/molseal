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
