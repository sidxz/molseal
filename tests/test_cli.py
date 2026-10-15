import json
import uuid

import pytest
import sqlalchemy as sa

from molseal import cli
from molseal.store import SqlStore
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
    for var in ("MOLSEAL_DATABASE_URL", "MOLSEAL_KEY", "MOLSEAL_TENANT"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["count", "CCO"]) == 2
    assert "--db" in capsys.readouterr().err


def _loaded(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    return common, src.with_name(src.name + ".ids.tsv"), uuid.UUID(common[common.index("--tenant") + 1])


def _check(argv, capsys):
    rc = cli.main(["absence-check", *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_absence_check_passes_and_finds_its_control(ctx, capsys):
    common, sidecar, _ = _loaded(ctx, capsys)
    rc, out = _check([str(sidecar), *common], capsys)
    assert rc == 0 and out["ok"] and out["db_hits"] == 0 and out["needles"] > 0
    assert out["control_found"] == {"db": True, "dump": None}
    rc, _ = _check([str(sidecar), *common], capsys)        # a second run replaces the planted row
    assert rc == 0


def test_absence_check_catches_a_plaintext_log(ctx, capsys, tmp_path):
    common, sidecar, _ = _loaded(ctx, capsys)
    leaked = sidecar.read_text().splitlines()[0].split("\t")[2].encode()      # an InChIKey
    log = tmp_path / "postgres.log"             # how PostgreSQL logs a bytea parameter
    log.write_text("LOG:  execute <unnamed>: INSERT INTO structures (...) VALUES ($1, ...)\n"
                   f"DETAIL:  parameters: $1 = '\\x{leaked.hex()}'\n")
    rc, out = _check([str(sidecar), "--log", str(log), *common], capsys)
    assert rc == 1 and not out["ok"] and out["log_hits"] == 1
    assert out["log_control"] == "not applicable"


def _fake_dump(path, tenant, ciphertext: bytes):
    path.write_text("COPY public.structures (tenant_id, id, row_version, data_key_version, nonce, ciphertext, "
                    "deleted, seq, updated_at) FROM stdin;\n"
                    f"{tenant}\t{cli.CONTROL_ID}\t1\t1\t\\\\x{'00' * 12}\t\\\\x{ciphertext.hex()}\tt\t0\t2026-10-13\n"
                    "\\.\n")


def test_absence_check_dump_leg_passes_when_it_holds_the_control(ctx, capsys, tmp_path):
    common, sidecar, tenant = _loaded(ctx, capsys)
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, cli.control_marker(tenant))
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 0 and out["ok"] and out["dump_hits"] == 0
    assert out["control_found"] == {"db": True, "dump": True}


def test_absence_check_dump_leg_fails_without_the_control(ctx, capsys, tmp_path):
    common, sidecar, tenant = _loaded(ctx, capsys)
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, b"some other bytes, not the control")
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 1 and not out["ok"] and out["control_found"]["dump"] is False


def test_absence_check_dump_leg_finds_a_hex_leak(ctx, capsys, tmp_path):
    common, sidecar, tenant = _loaded(ctx, capsys)
    smiles = max((l.split("\t")[1] for l in sidecar.read_text().splitlines()), key=len).encode()
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, cli.control_marker(tenant) + b"\x00" + smiles)
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 1 and out["dump_hits"] == 1 and out["control_found"]["dump"] is True


def test_absence_check_fails_when_pg_dump_is_missing(ctx, capsys, monkeypatch):
    common, sidecar, _ = _loaded(ctx, capsys)
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    rc = cli.main(["absence-check", str(sidecar), "--run-pg-dump", *common])
    captured = capsys.readouterr()
    assert rc == 1 and "pg_dump not found" in captured.err
    assert json.loads(captured.out)["ok"] is False


def test_leak_report_prints_numbers(ctx, capsys):
    common, src = ctx
    assert cli.main(["load", str(src), *common]) == 0
    capsys.readouterr()
    sidecar = src.with_name(src.name + ".ids.tsv")
    assert cli.main(["leak-report", str(sidecar), *common]) == 0
    out = capsys.readouterr().out
    assert '"pearson_r"' in out and '"predict_accuracy"' in out and '"n": 30' in out


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
