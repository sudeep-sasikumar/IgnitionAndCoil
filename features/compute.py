"""Per-symbol features (spec §5) from CLOSED bars only.

compute_features() is pure: the caller passes arrays already cut to the as-of time
(BarArrays.upto) plus as-of OI/funding values. The live engine and the backtester
both call this, so there is one implementation and no lookahead.
Levels/headroom (§5, last bullet) are added in M2.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from data.bars import BarArrays
from features import indicators as ind


@dataclass
class MarketInputs:
    """Non-kline inputs, already resolved as of the bar close."""
    oi_chg_15m: float | None = None
    oi_chg_1h: float | None = None
    oi_chg_4h: float | None = None
    funding_8h: float | None = None      # fraction; 0.0001 = 0.01%
    next_funding_ms: int | None = None


@dataclass
class Features:
    symbol: str
    as_of: int                           # close time of the last 5m bar used
    price: float
    ret_1h: float
    ret_4h: float
    ret_24h: float
    rs_1h: float
    rvol_5m: float
    rvol_15m: float
    vwap_24h: float
    ema20_15m: float
    ema50_15m: float
    ema20_1h: float
    ema50_1h: float
    ema50_1h_rising: bool
    atr_15m: float
    atr_1h: float
    atr_1h_pct: float
    bbw_1h: float
    bbw_pct_1h: float
    close_1h: float
    taker_buy_ratio_15m: float
    cvd_slope_1h: float                  # quote units per 5m bar
    cvd_slope_1h_norm: float             # slope / mean 5m quote volume
    deep_wicks_24h: int
    oi_chg_15m: float | None
    oi_chg_1h: float | None
    oi_chg_4h: float | None
    funding_8h: float | None
    next_funding_ms: int | None
    warm: bool = True                    # False if some lookback was too short
    notes: list[str] = field(default_factory=list)
    # short-side mirrors (short setups are backtest-only for now)
    ema50_1h_falling: bool = False
    deep_upper_wicks_24h: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _last(x: np.ndarray) -> float:
    return float(x[-1]) if len(x) and np.isfinite(x[-1]) else float("nan")


def _feats_15m(b15: BarArrays, cfg) -> dict:
    f, u = cfg.features, cfg.universe
    return {"e20_15": _last(ind.ema(b15.c, f.ema_fast)), "e50_15": _last(ind.ema(b15.c, f.ema_slow)),
            "atr15": _last(ind.atr(b15.h, b15.l, b15.c, u.atr_period))}


def _feats_1h(b1h: BarArrays, cfg) -> dict:
    f, u = cfg.features, cfg.universe
    ema50 = ind.ema(b1h.c, f.ema_slow)
    k = cfg.regime.ema_slope_bars_1h
    close_1h = float(b1h.c[-1]) if len(b1h) else float("nan")
    atr1h = _last(ind.atr(b1h.h, b1h.l, b1h.c, u.atr_period))
    bbw = ind.bb_width(b1h.c, f.bb_period, f.bb_mult)
    return {
        "e20_1h": _last(ind.ema(b1h.c, f.ema_fast)), "e50_1h": _last(ema50),
        "rising": bool(len(ema50) > k and np.isfinite(ema50[-1 - k]) and ema50[-1] > ema50[-1 - k]),
        "falling": bool(len(ema50) > k and np.isfinite(ema50[-1 - k]) and ema50[-1] < ema50[-1 - k]),
        "atr1h": atr1h, "close_1h": close_1h, "atr1h_pct": atr1h / close_1h * 100 if close_1h else float("nan"),
        "bbw_last": _last(bbw),
        "bbw_pct": ind.percentile_rank(bbw, f.bb_pct_lookback_1h) if len(bbw) else float("nan"),
        "bbw_short": np.count_nonzero(np.isfinite(bbw[-f.bb_pct_lookback_1h:])) < f.bb_pct_lookback_1h,
    }


def _memo(memo: dict | None, key: tuple, fn):
    """15m/1h indicators only change when those bars close: cache them by (symbol, tf, last bar
    open time, length). Pure functions of the bars, so a hit is always identical to a recompute."""
    if memo is None:
        return fn()
    hit = memo.get(key)
    if hit is None:
        if len(memo) > 20_000:
            memo.clear()
        hit = memo[key] = fn()
    return hit


def compute_features(symbol: str, b5: BarArrays, b15: BarArrays, b1h: BarArrays,
                     btc5: BarArrays | None, mk: MarketInputs, cfg, memo: dict | None = None) -> Features:
    f = cfg.features
    u = cfg.universe
    notes: list[str] = []
    c5 = b5.c
    price = float(c5[-1])

    ret_1h = ind.pct_change(c5, 12)
    ret_4h = ind.pct_change(c5, 48)
    ret_24h = ind.pct_change(c5, 288)
    btc_ret_1h = ind.pct_change(btc5.c, 12) if btc5 is not None and len(btc5) else float("nan")
    rs_1h = ret_1h - btc_ret_1h

    n5 = f.rvol_5m_lookback
    rvol_5m = float(b5.qv[-1] / b5.qv[-1 - n5:-1].mean()) if len(b5) > n5 and b5.qv[-1 - n5:-1].mean() > 0 else float("nan")
    n15 = f.rvol_15m_lookback
    rvol_15m = float(b15.qv[-1] / b15.qv[-1 - n15:-1].mean()) if len(b15) > n15 and b15.qv[-1 - n15:-1].mean() > 0 else float("nan")

    vw = ind.vwap(b5.h, b5.l, b5.c, b5.v, f.vwap_bars_5m)

    k15 = (symbol, "15m", int(b15.t[-1]) if len(b15) else 0, len(b15))
    k1h = (symbol, "1h", int(b1h.t[-1]) if len(b1h) else 0, len(b1h))
    m15 = _memo(memo, k15, lambda: _feats_15m(b15, cfg))
    m1h = _memo(memo, k1h, lambda: _feats_1h(b1h, cfg))
    if m1h["bbw_short"]:
        notes.append("bbw_pct<30d")

    nt = f.taker_ratio_bars_5m
    tot = b5.qv[-nt:].sum()
    taker_ratio = float(b5.tbqv[-nt:].sum() / tot) if tot > 0 else float("nan")

    nc = f.cvd_bars_5m
    delta = (2 * b5.tbqv[-nc:] - b5.qv[-nc:])      # buy - sell per bar
    cvd = np.cumsum(delta)
    cvd_slope = ind.linreg_slope(cvd)
    mean_q = b5.qv[-nc:].mean() if len(b5) >= nc else float("nan")
    cvd_norm = cvd_slope / mean_q if mean_q and mean_q > 0 else float("nan")

    wl = u.wick_lookback_bars_5m
    wick = ind.lower_wick_pct(b5.o[-wl:], b5.l[-wl:], b5.c[-wl:])
    deep = int(np.count_nonzero(wick >= u.deep_wick_pct))
    up_wick = ind.upper_wick_pct(b5.o[-wl:], b5.h[-wl:], b5.c[-wl:])
    deep_up = int(np.count_nonzero(up_wick >= u.deep_wick_pct))

    k = cfg.regime.ema_slope_bars_1h
    warm = len(b5) > 288 and len(b15) > max(n15, f.ema_slow) and len(b1h) > f.ema_slow + k
    if not warm:
        notes.append("warming")

    return Features(
        symbol=symbol, as_of=int(b5.tc[-1]), price=price,
        ret_1h=ret_1h, ret_4h=ret_4h, ret_24h=ret_24h, rs_1h=rs_1h,
        rvol_5m=rvol_5m, rvol_15m=rvol_15m, vwap_24h=vw,
        ema20_15m=m15["e20_15"], ema50_15m=m15["e50_15"], ema20_1h=m1h["e20_1h"], ema50_1h=m1h["e50_1h"],
        ema50_1h_rising=m1h["rising"], atr_15m=m15["atr15"], atr_1h=m1h["atr1h"], atr_1h_pct=m1h["atr1h_pct"],
        bbw_1h=m1h["bbw_last"], bbw_pct_1h=m1h["bbw_pct"], close_1h=m1h["close_1h"],
        taker_buy_ratio_15m=taker_ratio, cvd_slope_1h=cvd_slope, cvd_slope_1h_norm=cvd_norm, deep_wicks_24h=deep,
        oi_chg_15m=mk.oi_chg_15m, oi_chg_1h=mk.oi_chg_1h, oi_chg_4h=mk.oi_chg_4h,
        funding_8h=mk.funding_8h, next_funding_ms=mk.next_funding_ms, warm=warm, notes=notes,
        ema50_1h_falling=m1h["falling"], deep_upper_wicks_24h=deep_up,
    )


def is_nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))
