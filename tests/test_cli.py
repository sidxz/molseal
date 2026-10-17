import uuid

import pytest
import sqlalchemy as sa

from molseal import cli
from molseal.store import SqlStore, VersionConflict
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

    assert cli.main(["search", "[r5]", "--kind", "smarts", *common]) == 0
    capsys.readouterr()
    assert cli.main(["count", "c1ccccc1", "--generalized", "--chirality", *common]) == 0
    capsys.readouterr()
    assert cli.main(["similar", lines[0].split("\t")[1], "--threshold", "0.99", *common]) == 0
    row = capsys.readouterr().out.strip().splitlines()[0].split("\t")
    assert row[0] == lines[0].split("\t")[0] and float(row[1]) > 0.99


def test_missing_connection_options_fail_cleanly(monkeypatch, capsys):
    for var in ("MOLSEAL_DATABASE_URL", "MOLSEAL_KEY", "MOLSEAL_TENANT"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["count", "CCO"]) == 2
    assert "--db" in capsys.readouterr().err


def _with(common, flag, value):
    out = [*common]
    out[out.index(flag) + 1] = value
    return out


def test_rewrap_moves_a_tenant_to_a_new_key(ctx, capsys, tmp_path):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    new = tmp_path / "new.key"
    assert cli.main(["keygen", str(new)]) == 0
    assert cli.main(["rewrap", "--to", str(new), *common]) == 0
    assert "rewrapped 2 key rows" in capsys.readouterr().out
    assert cli.main(["count", "c1ccccc1", *_with(common, "--key", str(new))]) == 0
    assert int(capsys.readouterr().out.strip()) > 0
    assert cli.main(["count", "c1ccccc1", *common]) == 1          # the old key no longer opens the tenant
    assert "did not open" in capsys.readouterr().err


def test_rewrap_all_covers_every_tenant(ctx, capsys, tmp_path):
    common, src = ctx
    second = _with(common, "--tenant", str(uuid.uuid4()))
    assert cli.main(["load", str(src), *common]) == 0
    assert cli.main(["load", str(src), *second]) == 0
    capsys.readouterr()
    new = tmp_path / "new.key"
    assert cli.main(["keygen", str(new)]) == 0
    assert cli.main(["rewrap", "--to", str(new), "--all", *common]) == 0
    assert capsys.readouterr().out.count("rewrapped 2 key rows") == 2
    assert cli.main(["count", "c1ccccc1", *_with(second, "--key", str(new))]) == 0


def test_rewrap_all_on_an_empty_database_is_a_no_op(ctx, capsys, tmp_path):
    common, _ = ctx
    no_tenant = [a for i, a in enumerate(common) if a != "--tenant" and common[i - 1] != "--tenant"]
    assert cli.main(["rewrap", "--to", common[common.index("--key") + 1], "--all", *no_tenant]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "no tenants" in captured.err


def test_rewrap_all_continues_past_a_tenant_without_key_rows(ctx, capsys, tmp_path):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    SqlStore(sa.create_engine(common[common.index("--db") + 1])).ensure_tenant(uuid.UUID(int=0))
    new = tmp_path / "new.key"
    assert cli.main(["keygen", str(new)]) == 0
    capsys.readouterr()
    no_tenant = [a for i, a in enumerate(common) if a != "--tenant" and common[i - 1] != "--tenant"]
    assert cli.main(["rewrap", "--to", str(new), "--all", *no_tenant]) == 1
    out, err = capsys.readouterr()
    assert "rewrapped 2 key rows" in out and "00000000-0000-0000-0000-000000000000" in err
    assert cli.main(["count", "c1ccccc1", *_with(common, "--key", str(new))]) == 0


def test_rewrap_to_a_bad_provider_fails_and_keeps_the_old_key_working(ctx, capsys, tmp_path):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    bad = tmp_path / "bad.key"
    bad.write_bytes(b"short")
    bad.chmod(0o600)
    assert cli.main(["rewrap", "--to", str(bad), *common]) == 1
    assert "32 bytes" in capsys.readouterr().err
    assert cli.main(["count", "c1ccccc1", *common]) == 0


def test_key_check_passes_for_a_file_and_rejects_an_unknown_scheme(ctx, capsys, tmp_path):
    common, _ = ctx
    assert cli.main(["key-check", *common]) == 0
    assert capsys.readouterr().out.strip() == "FileKeyProvider: ok"
    assert cli.main(["key-check", *_with(common, "--key", "unknown:x")]) == 2
    assert "unknown" in capsys.readouterr().err
    assert cli.main(["key-check", "--key", str(tmp_path / "missing.key")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_evaluation_commands_moved_to_bench(ctx):
    common, _ = ctx
    for cmd in ("absence-check", "leak-report"):
        with pytest.raises(SystemExit):
            cli.main([cmd, "x.tsv", *common])
    with pytest.raises(SystemExit):
        cli.main(["search", "c1ccccc1", "--smarts", *common])


def test_rotate_keys_reencrypts_a_tenant_and_resume_after_is_a_no_op(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    assert cli.main(["rotate-keys", *common]) == 0
    assert "re-encrypted 30 rows" in capsys.readouterr().out
    assert cli.main(["rotate-keys", "--resume", *common]) == 0
    assert "re-encrypted 0 rows" in capsys.readouterr().out
    assert cli.main(["count", "c1ccccc1", *common]) == 0


def test_rotate_keys_all_continues_past_a_tenant_without_key_rows(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    SqlStore(sa.create_engine(common[common.index("--db") + 1])).ensure_tenant(uuid.UUID(int=0))
    capsys.readouterr()
    no_tenant = [a for i, a in enumerate(common) if a != "--tenant" and common[i - 1] != "--tenant"]
    assert cli.main(["rotate-keys", "--all", *no_tenant]) == 1
    out, err = capsys.readouterr()
    assert "re-encrypted 30 rows" in out and "00000000-0000-0000-0000-000000000000" in err


def test_rewrap_reports_a_key_row_conflict_instead_of_a_traceback(ctx, capsys, monkeypatch):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()

    def conflict(*a):
        raise VersionConflict("key rows changed since they were read; run again")
    monkeypatch.setattr(SqlStore, "replace_tenant_keys", conflict)
    assert cli.main(["rewrap", "--to", common[common.index("--key") + 1], *common]) == 1
    assert "run again" in capsys.readouterr().err

