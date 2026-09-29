"""No-lookahead: features at time T depend only on bars closed by T."""
import dataclasses
import math

import numpy as np

from data.bars import BarArrays, BarSeries
from exchange.models import Bar
from features.compute import MarketInputs, compute_features

M5, M15, H1 = 300_000, 900_000, 3_600_000


def synth(step, n, seed):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    bars = []
    for i in range(n):
        o = c[i - 1] if i else c[0]
        v = float(rng.uniform(50, 150))
        bars.append(Bar(i * step, o, max(o, c[i]) * 1.002, min(o, c[i]) * 0.998, c[i], v, v * c[i],
                        v * 0.5, v * c[i] * 0.5, 10, (i + 1) * step))
    return bars


def feats_equal(a, b):
    for k, v in dataclasses.asdict(a).items():
        w = getattr(b, k)
        if isinstance(v, float) and math.isnan(v):
            assert math.isnan(w), k
        else:
            assert v == w, k


def test_upto_cuts_at_close_time():
    s = BarSeries(100)
    s.merge(synth(M5, 10, 1))
    a = s.arrays()
    assert len(a.upto(3 * M5)) == 3          # bar 2 closes exactly at 3*M5 -> included
    assert len(a.upto(3 * M5 - 1)) == 2


def test_features_ignore_future_bars(cfg):
    b5, b15, b1h = synth(M5, 1500, 1), synth(M15, 600, 2), synth(H1, 900, 3)
    btc = synth(M5, 1500, 4)
    T = 1200 * M5                              # an as-of time well inside the data
    full = [BarArrays.from_bars(x) for x in (b5, b15, b1h, btc)]
    f_full = compute_features("X", *(a.upto(T) for a in full[:3]), full[3].upto(T), MarketInputs(), cfg)

    # Same computation with every bar that closes after T physically removed.
    cut = [BarArrays.from_bars([b for b in x if b.tc <= T]) for x in (b5, b15, b1h, btc)]
    f_cut = compute_features("X", *cut[:3], cut[3], MarketInputs(), cfg)
    feats_equal(f_full, f_cut)

    # And scrambling the future must not change anything.
    rng = np.random.default_rng(9)
    scrambled = [BarArrays.from_bars([b if b.tc <= T else b._replace(c=b.c * rng.uniform(0.5, 2), h=b.h * 3,
                                                                    v=b.v * 50) for b in x])
                 for x in (b5, b15, b1h, btc)]
    f_scr = compute_features("X", *(a.upto(T) for a in scrambled[:3]), scrambled[3].upto(T), MarketInputs(), cfg)
    feats_equal(f_full, f_scr)
    assert f_full.as_of == T


def test_signal_pipeline_ignores_future_bars(cfg):
    """Levels, setups, score and plan at T are identical whatever happens after T."""
    from signals.engine import SignalEngine

    b5, b15, b1h, b4h = synth(M5, 1500, 1), synth(M15, 600, 2), synth(H1, 900, 3), synth(4 * H1, 400, 5)
    T = 1200 * M5

    def run(series):
        a = [BarArrays.from_bars(x).upto(T) for x in series]
        f = compute_features("X", a[0], a[1], a[2], a[0], MarketInputs(oi_chg_1h=3.0, oi_chg_4h=4.0), cfg)
        row, sigs, _ = SignalEngine(cfg).evaluate("X", T, f, a[0], a[1], a[2], a[3], None, None, f.price, 4)
        lm = SignalEngine(cfg).levels("X", a[2], a[3])
        return (row.score, row.state, row.n_pass, row.headroom_pct, [(lv.price, lv.kind) for lv in lm.levels],
                [s.to_record() for s in sigs])

    base = run([b5, b15, b1h, b4h])
    rng = np.random.default_rng(11)
    future = [[b if b.tc <= T else b._replace(h=b.h * rng.uniform(1, 3), v=b.v * 100) for b in x]
              for x in (b5, b15, b1h, b4h)]
    assert run(future) == base


def test_feature_memo_is_transparent(cfg):
    """The 15m/1h memo used live and in backtests must never change a result."""
    b5, b15, b1h = synth(M5, 1500, 1), synth(M15, 600, 2), synth(H1, 900, 3)
    arr = [BarArrays.from_bars(x) for x in (b5, b15, b1h)]
    memo = {}
    for T in range(1100 * M5, 1200 * M5, 7 * M5):     # successive bars, memo reused across them
        cut = [a.upto(T) for a in arr]
        with_memo = compute_features("X", *cut, cut[0], MarketInputs(), cfg, memo=memo)
        plain = compute_features("X", *cut, cut[0], MarketInputs(), cfg)
        feats_equal(with_memo, plain)
    assert memo   # it was actually used
