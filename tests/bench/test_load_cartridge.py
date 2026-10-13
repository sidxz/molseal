import os
from pathlib import Path

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
    engine = sa.create_engine(url)
    lc.ensure_schema(engine)
    yield engine
    engine.dispose()


def test_load_parses_counts_rejected_and_pins_settings(cart_engine, tmp_path):
    slice_path = tmp_path / "slice_21.tsv"
    rows = [(f"T{i}", s, f"K{i}") for i, s in enumerate(molecules()[:20])] + [("T20", "C1CC", "K20")]
    slice_path.write_text("".join("\t".join(r) + "\n" for r in rows))
    with cart_engine.begin() as c:
        c.execute(sa.text("DELETE FROM mols WHERE chembl_id LIKE 'T%'"))
    stats = lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)
    assert stats["rows"] == 21 and stats["rejected"] == 1
    assert stats["morgan_fp_size"] == "2048" and stats["do_chiral_sss"] == "off"
    assert (tmp_path / "cartridge_rejected_21.txt").read_text().strip() == str(row_id("T20"))
    with cart_engine.connect() as c:
        n = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND mfp IS NOT NULL")).scalar_one()
        hit = c.execute(sa.text("SELECT count(*) FROM mols WHERE chembl_id LIKE 'T%' AND m @> mol_from_smiles('c1ccccc1'::cstring)")).scalar_one()
    assert n == 20 and hit > 0
    assert lc.load(slice_path, cart_engine, batch=7, results_dir=tmp_path)["rows"] == 21   # idempotent
