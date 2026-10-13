import uuid

import pytest

from molseal import cli
from tests.mols import molecules


@pytest.fixture
def ctx(tmp_path):
    key = tmp_path / "kek.key"
    assert cli.main(["keygen", str(key)]) == 0
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    tenant = str(uuid.uuid4())
    src = tmp_path / "mols.smi"
    src.write_text("\n".join(molecules()[:30] + ["garbage(", "# comment"]) + "\n")
    common = ["--db", db, "--key", str(key), "--tenant", tenant, "--snapshot-dir", str(tmp_path / "snap")]
    return common, src


def test_keygen_refuses_overwrite(tmp_path):
    key = tmp_path / "k"
    assert cli.main(["keygen", str(key)]) == 0
    assert cli.main(["keygen", str(key)]) != 0


def test_load_search_count_similar(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    out = capsys.readouterr().out
    assert "loaded 30" in out and "skipped 1" in out
    sidecar = src.with_name(src.name + ".ids.tsv")
    lines = sidecar.read_text().splitlines()
    assert len(lines) == 30 and len(lines[0].split("\t")) == 3

    assert cli.main(["count", "c1ccccc1", *common]) == 0
    n = int(capsys.readouterr().out.strip())
    assert n > 0

    assert cli.main(["search", "c1ccccc1", "--max", "3", *common]) == 0
    ids = capsys.readouterr().out.split()
    assert len(ids) == 3 and all(uuid.UUID(i) for i in ids)

    assert cli.main(["search", "[r5]", "--smarts", *common]) == 0
    capsys.readouterr()
    assert cli.main(["similar", lines[0].split("\t")[1], "--threshold", "0.99", *common]) == 0
    row = capsys.readouterr().out.strip().splitlines()[0].split("\t")
    assert row[0] == lines[0].split("\t")[0] and float(row[1]) > 0.99


def test_missing_connection_options_fail_cleanly(monkeypatch, capsys):
    for var in ("MOLSEAL_DATABASE_URL", "MOLSEAL_KEY_FILE", "MOLSEAL_TENANT"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["count", "CCO"]) == 2
    assert "--db" in capsys.readouterr().err
