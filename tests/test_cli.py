import json
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
