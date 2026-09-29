"""Pure indicator functions on numpy arrays.

Conventions match TradingView (so the Pine companion lines up):
- EMA seeded with the SMA of the first `n` values (ta.ema)
- RMA/Wilder seeded with SMA (ta.rma); ATR = RMA of true range (ta.atr)
- Bollinger stdev is the population stdev (ta.stdev default)
Warm-up values are NaN.
"""
from __future__ import annotations

import numpy as np


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _recursive(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    prev = float(np.mean(x[:n]))
    vals = [prev]
    beta = 1 - alpha
    for v in x[n:].tolist():          # plain floats: same arithmetic, far faster than numpy scalars
        prev = alpha * v + beta * prev
        vals.append(prev)
    out[n - 1:] = vals
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive(x, n, 1.0 / n)


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev_c = np.roll(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    if len(tr):
        tr[0] = h[0] - l[0]
    return tr


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    return rma(true_range(h, l, c), n)


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    w = np.lib.stride_tricks.sliding_window_view(x.astype(float), n)
    out[n - 1:] = w.std(axis=1)  # population (ddof=0), like ta.stdev
    return out


def bb_width(c: np.ndarray, n: int, mult: float) -> np.ndarray:
    """(upper - lower) / basis, as a fraction (TradingView ta.bbw is this x100)."""
    basis = sma(c, n)
    dev = rolling_std(c, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (2 * mult * dev) / basis


def percentile_rank(x: np.ndarray, lookback: int) -> float:
    """Percent (0-100) of the last `lookback` finite values that are <= the latest value."""
    w = x[-lookback:]
    w = w[np.isfinite(w)]
    if len(w) == 0 or not np.isfinite(x[-1]):
        return float("nan")
    return float(np.count_nonzero(w <= x[-1]) / len(w) * 100.0)


def vwap(h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray, n: int) -> float:
    """Rolling VWAP of the last n bars using typical price (h+l+c)/3."""
    if len(c) == 0:
        return float("nan")
    tp = (h[-n:] + l[-n:] + c[-n:]) / 3.0
    vol = v[-n:]
    s = vol.sum()
    return float((tp * vol).sum() / s) if s > 0 else float("nan")


def pct_change(c: np.ndarray, bars: int) -> float:
    """% change of the last close vs the close `bars` bars earlier."""
    if len(c) <= bars or c[-1 - bars] == 0:
        return float("nan")
    return float((c[-1] / c[-1 - bars] - 1.0) * 100.0)


def linreg_slope(y: np.ndarray) -> float:
    """Least-squares slope per bar."""
    n = len(y)
    if n < 2:
        return float("nan")
    x = np.arange(n, dtype=float)
    x -= x.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())


def lower_wick_pct(o: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    body_low = np.minimum(o, c)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (body_low - l) / body_low * 100.0
