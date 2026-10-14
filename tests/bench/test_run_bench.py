import json
import os

import pytest

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


def test_summarize_p50_is_the_true_median():
    rows = [{"side": "m", "where": "vm", "mode": "warm", "threads": 1, "op": "count", "capped": False, "median_s": x}
            for x in (1.0, 2.0, 3.0, 10.0)]
    assert "| 4 | 2.5000 | 10.0000 |" in rb.summarize(rows)


def test_check_parity_refuses_missing_failed_or_stale_parity(tmp_path):
    with pytest.raises(SystemExit, match="have not passed"):
        rb.check_parity(tmp_path, "9")
    parity = tmp_path / "parity_9.json"
    parity.write_text(json.dumps({"ok": False}))
    with pytest.raises(SystemExit, match="have not passed"):
        rb.check_parity(tmp_path, "9")
    parity.write_text(json.dumps({"ok": True}))
    load = tmp_path / "load_molseal_9.json"
    load.write_text("{}")
    os.utime(parity, (1000, 1000))
    with pytest.raises(SystemExit, match="older than the latest load"):
        rb.check_parity(tmp_path, "9")
    os.utime(parity, None)
    os.utime(load, (1000, 1000))
    rb.check_parity(tmp_path, "9")                     # fresh parity passes
