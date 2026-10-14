# src/molseal/metrics.py
"""Similarity metrics as pure functions of three bit counts.

`c` is the per-row intersection count (array), `a` the query's bit count (int), `b` the per-row bit
count (array). Each metric returns float64 scores and gives 0.0 where its denominator is 0. Any
function with this shape is a valid metric for StructureIndex.similar / Vault.similar.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

Metric = Callable[[np.ndarray, int, np.ndarray], np.ndarray]


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype=np.float64)


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    return np.divide(num, den, out=np.zeros_like(num), where=den > 0)


def tanimoto(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(c, a + b - c)


def dice(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(2 * c, a + b)


def cosine(c, a, b) -> np.ndarray:
    c, b = _f(c), _f(b)
    return _ratio(c, np.sqrt(a * b))


def tversky(query_weight: float, target_weight: float) -> Metric:
    """`query_weight` (alpha) weights bits only in the query; `target_weight` (beta) weights bits only
    in the target. tversky(1, 0) = c / a: how much of the query is inside the target."""
    def metric(c, a, b) -> np.ndarray:
        c, b = _f(c), _f(b)
        return _ratio(c, query_weight * (a - c) + target_weight * (b - c) + c)
    metric.__name__ = f"tversky({query_weight}, {target_weight})"
    return metric
