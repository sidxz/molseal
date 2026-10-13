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
