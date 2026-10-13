import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from bench import load_molseal as lm
from molseal import keys
from molseal.store import SqlStore
from tests.mols import molecules


@pytest.fixture
def env(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'b.db'}")
    store = SqlStore(engine)
    store.create_tables()
    kp = tmp_path / "kek.key"
    keys.generate_key_file(kp)
    slice_path = tmp_path / "slice_25.tsv"
    rows = molecules()[:25]
    slice_path.write_text("".join(f"CHEMBL{i}\t{s}\tKEY{i}\n" for i, s in enumerate(rows)))
    return store, keys.FileKeyProvider(kp), slice_path


def test_ids_are_deterministic():
    assert lm.row_id("CHEMBL25") == lm.row_id("CHEMBL25")
    assert lm.row_id("CHEMBL25") != lm.row_id("CHEMBL26")


def test_load_writes_sidecar_progress_and_stats(env):
    store, kp, slice_path = env
    t = uuid4()
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["rows"] == 25 and stats["chunks"] == 3 and stats["resumed_from"] == 0
    assert store.tenant_state(t) == (25, 25)
    sidecar = slice_path.with_name(slice_path.name + ".ids.tsv").read_text().splitlines()
    assert len(sidecar) == 25 and sidecar[0].split("\t")[0] == str(lm.row_id("CHEMBL0"))
    assert json.loads(slice_path.with_name(slice_path.name + ".progress.json").read_text()) == {"done_chunks": 3}


def test_resume_after_a_crash_between_commit_and_progress_does_not_duplicate(env):
    store, kp, slice_path = env
    t = uuid4()
    lm.load(slice_path, store, kp, t, chunk=10)
    progress = slice_path.with_name(slice_path.name + ".progress.json")
    progress.write_text(json.dumps({"done_chunks": 1}))          # pretend chunks 2 and 3 were never recorded
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["resumed_from"] == 1 and stats["rows"] == 15
    assert store.tenant_state(t)[1] == 25                          # live count unchanged
    assert store.get_structure(t, lm.row_id("CHEMBL24")).row_version == 2
    assert store.get_structure(t, lm.row_id("CHEMBL0")).row_version == 1
    assert json.loads(progress.read_text()) == {"done_chunks": 3}


def test_fully_loaded_slice_is_a_no_op(env):
    store, kp, slice_path = env
    t = uuid4()
    lm.load(slice_path, store, kp, t, chunk=10)
    stats = lm.load(slice_path, store, kp, t, chunk=10)
    assert stats["rows"] == 0 and stats["resumed_from"] == 3
