import pytest

from molseal import payload


def test_encode_is_canonical_and_round_trips():
    p = payload.Payload(smiles="CCO", keys={"inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N", "a": "b"})
    b = payload.encode(p)
    assert b == b'{"deleted":false,"keys":{"a":"b","inchikey":"LFQSCWFLJHTTHZ-UHFFFAOYSA-N"},"smiles":"CCO","v":1}'
    assert payload.decode(b) == p


def test_tombstone_round_trips():
    p = payload.Payload(smiles="", keys={}, deleted=True)
    assert payload.decode(payload.encode(p)) == p


@pytest.mark.parametrize("bad", [b"not json", b'{"v":2,"smiles":"C","keys":{},"deleted":false}', b'{"v":1}', b"[]"])
def test_decode_rejects_garbage_and_unknown_versions(bad):
    with pytest.raises(payload.PayloadError):
        payload.decode(bad)
