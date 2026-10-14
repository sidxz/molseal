# tests/test_metrics.py
import numpy as np
import pytest
from rdkit import DataStructs

from molseal import metrics


def test_tversky_weights_must_be_in_unit_range():
    for bad in ((-0.5, 0), (0, 1.5)):
        with pytest.raises(ValueError):
            metrics.tversky(*bad)
    metrics.tversky(0, 0)
    metrics.tversky(1, 1)


def _bv(bits: int, rng: np.random.Generator) -> DataStructs.ExplicitBitVect:
    bv = DataStructs.ExplicitBitVect(2048)
    for i in rng.choice(2048, size=bits, replace=False):
        bv.SetBit(int(i))
    return bv


@pytest.fixture(scope="module")
def pairs():
    rng = np.random.default_rng(7)
    q = _bv(40, rng)
    targets = [_bv(int(n), rng) for n in rng.integers(5, 120, size=50)]
    qbits = set(q.GetOnBits())
    c = np.array([len(qbits & set(t.GetOnBits())) for t in targets])
    b = np.array([t.GetNumOnBits() for t in targets])
    return q, targets, c, q.GetNumOnBits(), b


@pytest.mark.parametrize("ours,ref", [
    (metrics.tanimoto, DataStructs.TanimotoSimilarity),
    (metrics.dice, DataStructs.DiceSimilarity),
    (metrics.cosine, DataStructs.CosineSimilarity),
    (metrics.tversky(1.0, 0.0), lambda x, y: DataStructs.TverskySimilarity(x, y, 1.0, 0.0)),
    (metrics.tversky(0.3, 0.7), lambda x, y: DataStructs.TverskySimilarity(x, y, 0.3, 0.7)),
])
def test_metrics_match_rdkit(pairs, ours, ref):
    q, targets, c, a, b = pairs
    got = ours(c, a, b)
    expected = np.array([ref(q, t) for t in targets])
    assert got.dtype == np.float64 and np.allclose(got, expected, atol=1e-9)


def test_tversky_weights_are_query_first(pairs):
    q, targets, c, a, b = pairs
    assert not np.allclose(metrics.tversky(1, 0)(c, a, b), metrics.tversky(0, 1)(c, a, b))
    # a query that sits entirely inside a larger target scores 1.0 with (1, 0) and less with (0, 1)
    assert metrics.tversky(1, 0)(np.array([a]), a, np.array([a + 10]))[0] == 1.0
    assert metrics.tversky(0, 1)(np.array([a]), a, np.array([a + 10]))[0] < 1.0
    assert metrics.tversky(1, 0).__name__ == "tversky(1, 0)"


def test_zero_denominators_score_zero_and_unsigned_inputs_do_not_wrap():
    c, b = np.array([0, 0], dtype=np.uint64), np.array([0, 5], dtype=np.uint64)
    for m in (metrics.tanimoto, metrics.dice, metrics.cosine, metrics.tversky(1, 0)):
        assert m(c, 0, b).tolist() == [0.0, 0.0]
    # c > a + b can only come from a caller bug; unsigned inputs must not wrap into a huge score (0.0, as the denominator is negative)
    assert metrics.tanimoto(np.array([3], dtype=np.uint64), 1, np.array([1], dtype=np.uint64))[0] == 0.0
