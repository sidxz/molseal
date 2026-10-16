import os

import pytest
import sqlalchemy as sa

from bench import load_cartridge as lc
from bench.load_molseal import row_id
from tests.mols import molecules


@pytest.fixture(scope="module")
def cart_engine():
    url = os.environ.get("MOLSEAL_CARTRIDGE_URL")
    if not url:
        pytest.skip("MOLSEAL_CARTRIDGE_URL not set")
    # a schema of its own, so the test never touches a loaded bench table
    engine = sa.create_engine(url, connect_args={"options": "-csearch_path=lc_test,public"})
    with engine.begin() as c:
        c.execute(sa.text("DROP SCHEMA IF EXISTS lc_test CASCADE"))
        c.execute(sa.text("CREATE SCHEMA lc_test"))
    yield engine
    with engine.begin() as c:
        c.execute(sa.text("DROP SCHEMA lc_test CASCADE"))
    engine.dispose()


def test_load_parses_counts_rejected_and_pins_settings(cart_engine, tmp_path):
    slice_path = tmp_path / "slice_21.tsv"
    rows = [(f"T{i}", s, f"K{i}") for i, s in enumerate(molecules()[:20])] + [("T20", "C1CC", "K20")]
    slice_path.write_text("".join("\t".join(r) + "\n" for r in rows))
    stats = lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)
    assert stats["rows"] == 21 and stats["rejected"] == 1
    assert stats["morgan_fp_size"] == "2048" and stats["do_chiral_sss"] == "off"
    assert (tmp_path / "cartridge_rejected_21.txt").read_text().strip() == str(row_id("T20"))
    with cart_engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND mfp IS NOT NULL")).scalar_one()
        hit = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND m @> mol_from_smiles('c1ccccc1'::cstring)")).scalar_one()
    assert n == 20 and hit > 0
    with pytest.raises(SystemExit, match="not empty"):        # a second load refuses instead of no-op inserts
        lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)
