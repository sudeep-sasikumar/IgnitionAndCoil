"""Indicators checked against hand-computed values and an independent pandas implementation."""
import math

import numpy as np
import pandas as pd
import pytest

from features import indicators as ind

nan = float("nan")


def assert_arr(a, b, tol=1e-4):
    assert len(a) == len(b)
    for x, y in zip(a, b):
        if math.isnan(y):
            assert math.isnan(x)
        else:
            assert x == pytest.approx(y, abs=tol)


def test_sma():
    assert_arr(ind.sma(np.array([1, 2, 3, 4, 5.0]), 3), [nan, nan, 2, 3, 4])


def test_ema_known_values():
    # seed = SMA(1,2,3) = 2; alpha = 0.5 -> 3, 4, 5
    assert_arr(ind.ema(np.arange(1, 7, dtype=float), 3), [nan, nan, 2, 3, 4, 5])


def test_rma_known_values():
    # seed 2; alpha 1/3 -> 8/3, 31/9, 116/27
    assert_arr(ind.rma(np.arange(1, 7, dtype=float), 3), [nan, nan, 2, 8 / 3, 31 / 9, 116 / 27])


def test_atr_known_values_with_gap():
    h = np.array([10, 11, 12, 11.5, 15.0])
    l = np.array([8, 9, 9.5, 10, 14.0])
    c = np.array([9, 10, 11, 10.5, 14.5])
    assert_arr(ind.true_range(h, l, c), [2, 2, 2.5, 1.5, 4.5])
    assert_arr(ind.atr(h, l, c, 3), [nan, nan, 13 / 6, 35 / 18, 151 / 54])


def test_ema_matches_pandas_on_random_data():
    rng = np.random.default_rng(7)
    x = 100 + rng.standard_normal(500).cumsum()
    n = 20
    ours = ind.ema(x, n)
    s = pd.Series(x[n - 1:].copy())
    s.iloc[0] = x[:n].mean()
    ref = s.ewm(span=n, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(ours[n - 1:], ref, rtol=1e-12)


def test_rma_matches_pandas_on_random_data():
    rng = np.random.default_rng(3)
    x = rng.random(300) * 5
    n = 14
    s = pd.Series(x[n - 1:].copy())
    s.iloc[0] = x[:n].mean()
    ref = s.ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(ind.rma(x, n)[n - 1:], ref, rtol=1e-12)


def test_bb_width_population_std():
    # mean 3, population std sqrt(2): width = 4*sqrt(2)/3
    w = ind.bb_width(np.array([1, 2, 3, 4, 5.0]), 5, 2.0)
    assert w[-1] == pytest.approx(4 * math.sqrt(2) / 3)
    rng = np.random.default_rng(1)
    x = 50 + rng.standard_normal(200).cumsum()
    s = pd.Series(x)
    ref = (4 * s.rolling(20).std(ddof=0) / s.rolling(20).mean()).to_numpy()
    np.testing.assert_allclose(ind.bb_width(x, 20, 2.0)[19:], ref[19:], rtol=1e-9)


def test_percentile_rank():
    assert ind.percentile_rank(np.array([1, 2, 3, 4, 5.0]), 5) == 100
    assert ind.percentile_rank(np.array([5, 4, 3, 2, 1.0]), 5) == 20
    assert ind.percentile_rank(np.array([nan, 1, 3, 2.0]), 4) == pytest.approx(200 / 3)


def test_vwap_typical_price():
    h, l, c, v = map(np.array, ([2, 4.0], [0, 2.0], [1, 3.0], [1, 3.0]))
    assert ind.vwap(h, l, c, v, 2) == pytest.approx(2.5)
    assert ind.vwap(h, l, c, v, 1) == pytest.approx(3.0)


def test_pct_change_and_slope():
    c = np.array([100, 101, 102, 110.0])
    assert ind.pct_change(c, 3) == pytest.approx(10.0)
    assert math.isnan(ind.pct_change(c, 4))
    assert ind.linreg_slope(np.array([0, 2, 4, 6.0])) == pytest.approx(2.0)


def test_lower_wick_pct():
    w = ind.lower_wick_pct(np.array([100.0, 101]), np.array([98.0, 100]), np.array([101.0, 100]))
    assert_arr(w, [2.0, 0.0])
