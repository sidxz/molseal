"""bench.leak: the absence check and the leak report. Both read a tenant that the CLI loaded."""

import json
import uuid

import pytest

from bench import leak
from molseal import cli
from tests.mols import molecules


@pytest.fixture
def loaded(tmp_path, capsys):
    """A tenant loaded through the CLI. Returns the bench.leak connection args, the sidecar, the tenant."""
    key = tmp_path / "kek.key"
    assert cli.main(["keygen", str(key)]) == 0
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    tenant = uuid.uuid4()
    src = tmp_path / "mols.smi"
    src.write_text("\n".join(molecules()[:30]) + "\n")
    assert cli.main(["load", str(src), "--db", db, "--key", str(key), "--tenant", str(tenant)]) == 0
    capsys.readouterr()
    return ["--db", db, "--tenant", str(tenant)], src.with_name(src.name + ".ids.tsv"), tenant


def _check(argv, capsys):
    rc = leak.main(["absence-check", *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_absence_check_passes_and_finds_its_control(loaded, capsys):
    common, sidecar, _ = loaded
    rc, out = _check([str(sidecar), *common], capsys)
    assert rc == 0 and out["ok"] and out["db_hits"] == 0 and out["needles"] > 0
    assert out["control_found"] == {"db": True, "dump": None}
    rc, _ = _check([str(sidecar), *common], capsys)        # a second run replaces the planted row
    assert rc == 0


def test_absence_check_catches_a_plaintext_log(loaded, capsys, tmp_path):
    common, sidecar, _ = loaded
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
                    f"{tenant}\t{leak.CONTROL_ID}\t1\t1\t\\\\x{'00' * 12}\t\\\\x{ciphertext.hex()}\tt\t0\t2026-10-13\n"
                    "\\.\n")


def test_absence_check_dump_leg_passes_when_it_holds_the_control(loaded, capsys, tmp_path):
    common, sidecar, tenant = loaded
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, leak.control_marker(tenant))
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 0 and out["ok"] and out["dump_hits"] == 0
    assert out["control_found"] == {"db": True, "dump": True}


def test_absence_check_dump_leg_fails_without_the_control(loaded, capsys, tmp_path):
    common, sidecar, tenant = loaded
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, b"some other bytes, not the control")
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 1 and not out["ok"] and out["control_found"]["dump"] is False


def test_absence_check_dump_leg_finds_a_hex_leak(loaded, capsys, tmp_path):
    common, sidecar, tenant = loaded
    smiles = max((l.split("\t")[1] for l in sidecar.read_text().splitlines()), key=len).encode()
    dump = tmp_path / "plain.sql"
    _fake_dump(dump, tenant, leak.control_marker(tenant) + b"\x00" + smiles)
    rc, out = _check([str(sidecar), "--dump", str(dump), *common], capsys)
    assert rc == 1 and out["dump_hits"] == 1 and out["control_found"]["dump"] is True


def test_absence_check_fails_when_pg_dump_is_missing(loaded, capsys, monkeypatch):
    common, sidecar, _ = loaded
    monkeypatch.setattr(leak.shutil, "which", lambda _: None)
    rc = leak.main(["absence-check", str(sidecar), "--run-pg-dump", *common])
    captured = capsys.readouterr()
    assert rc == 1 and "pg_dump not found" in captured.err
    assert json.loads(captured.out)["ok"] is False


def test_leak_report_prints_numbers(loaded, capsys):
    common, sidecar, _ = loaded
    assert leak.main(["leak-report", str(sidecar), *common]) == 0
    out = capsys.readouterr().out
    assert '"pearson_r"' in out and '"predict_accuracy"' in out and '"n": 30' in out
