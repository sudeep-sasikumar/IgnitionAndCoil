"""Shared synthetic data builders for tests."""
from __future__ import annotations

from data.bars import BarArrays
from exchange.models import Bar
from features.compute import Features

M5 = 300_000


def features(**over) -> Features:
    """A Features object that passes every Ignition condition by default."""
    base = dict(
        symbol="TSTUSDT", as_of=1000 * M5, price=102.0, ret_1h=2.5, ret_4h=0.5, ret_24h=6.0, rs_1h=2.0,
        rvol_5m=5.0, rvol_15m=3.0, vwap_24h=100.0, ema20_15m=101.0, ema50_15m=100.0, ema20_1h=100.5,
        ema50_1h=99.0, ema50_1h_rising=True, atr_15m=0.8, atr_1h=1.5, atr_1h_pct=1.5, bbw_1h=0.02,
        bbw_pct_1h=10.0, close_1h=101.0, taker_buy_ratio_15m=0.62, cvd_slope_1h=1000.0, cvd_slope_1h_norm=0.3,
        deep_wicks_24h=0, oi_chg_15m=1.0, oi_chg_1h=3.0, oi_chg_4h=4.0, funding_8h=0.00005,
        next_funding_ms=None, warm=True, notes=[])
    base.update(over)
    return Features(**base)


def breakout_bars(n: int = 200, base: float = 100.0, last=(100.5, 102.2, 100.3, 102.0)) -> BarArrays:
    """n-1 flat bars with highs at base+0.5, then a breakout candle (o, h, l, c)."""
    bars = [Bar(i * M5, base, base + 0.5, base - 0.5, base, 10, 10 * base, 5, 5 * base, 1, (i + 1) * M5)
            for i in range(n - 1)]
    o, h, l, c = last
    bars.append(Bar((n - 1) * M5, o, h, l, c, 50, 50 * c, 30, 30 * c, 1, n * M5))
    return BarArrays.from_bars(bars)


def flat_bars(n: int, step: int, price: float = 100.0, spread: float = 0.5) -> BarArrays:
    return BarArrays.from_bars([Bar(i * step, price, price + spread, price - spread, price, 10, 10 * price, 5,
                                    5 * price, 1, (i + 1) * step) for i in range(n)])
