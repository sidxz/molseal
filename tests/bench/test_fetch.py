import gzip
import json

from bench import fetch_chembl as fc

HEADER = "chembl_id\tcanonical_smiles\tstandard_inchi\tstandard_inchi_key\n"
ROWS = [
    ("CHEMBL1", "OCC", "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"),
    ("CHEMBL2", "garbage(", "InChI=1S/x", "XXXXXXXXXXXXXX-XXXXXXXXXX-X"),
    ("CHEMBL3", "C1=CC=CC=C1", "InChI=1S/C6H6/c1-2-4-6-5-3-1/h1-6H", "UHOVQNZJYSORNB-UHFFFAOYSA-N"),
]


def write_chemreps(path):
    with gzip.open(path, "wt") as f:
        f.write(HEADER)
        for r in ROWS:
            f.write("\t".join(r) + "\n")


def test_iter_chemreps_yields_id_smiles_and_key(tmp_path):
    p = tmp_path / "chemreps.txt.gz"
    write_chemreps(p)
    assert list(fc.iter_chemreps(p)) == [(r[0], r[1], r[3]) for r in ROWS]


def test_canonicalize_rows_canonicalizes_and_marks_garbage():
    out = fc.canonicalize_rows([(r[0], r[1], r[3]) for r in ROWS])
    assert out[0] == ("CHEMBL1", "CCO", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N")
    assert out[1] is None
    assert out[2] == ("CHEMBL3", "c1ccccc1", "UHOVQNZJYSORNB-UHFFFAOYSA-N")


def test_build_slice_takes_first_n_and_writes_meta(tmp_path):
    src = tmp_path / "chemreps.txt.gz"
    write_chemreps(src)
    out = tmp_path / "slice_2.tsv"
    meta = fc.build_slice(src, 2, out, workers=1)
    assert meta == {"requested": 2, "loaded": 1, "skipped": 1}
    assert out.read_text() == "CHEMBL1\tCCO\tLFQSCWFLJHTTHZ-UHFFFAOYSA-N\n"
    assert json.loads(out.with_suffix(".meta.json").read_text())["loaded"] == 1


def test_download_skips_existing_file(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(b"already here")
    assert fc.download("http://127.0.0.1:9/never", p) == p
    assert p.read_bytes() == b"already here"
