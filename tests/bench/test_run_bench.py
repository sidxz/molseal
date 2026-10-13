from bench import run_bench as rb


def test_percentiles_and_median():
    xs = [5, 1, 4, 2, 3]
    assert rb.median(xs) == 3
    assert rb.percentile(xs, 50) == 3
    assert rb.percentile(xs, 95) == 5
    assert rb.percentile([7], 95) == 7


def test_time_calls_warms_up_then_takes_the_median():
    calls = []
    def fn():
        calls.append(1)
    t = rb.time_calls(fn, repeats=3)
    assert len(calls) == 4 and t >= 0.0


def test_summarize_groups_rows_into_a_table_with_p50_and_p95():
    rows = [
        {"side": "molseal", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.010, "query_id": "a", "hits": 5},
        {"side": "molseal", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.030, "query_id": "b", "hits": 50},
        {"side": "cartridge", "where": "vm", "mode": "warm", "threads": 1, "op": "substructure", "capped": False, "median_s": 0.020, "query_id": "a", "hits": 5},
    ]
    md = rb.summarize(rows)
    assert "| molseal | vm | warm | 1 | substructure | uncapped | 2 |" in md
    assert "cartridge" in md and "p95" in md
