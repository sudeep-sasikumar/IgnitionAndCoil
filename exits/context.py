"""Inputs the exit engine needs at each closed 15m bar, and bar-by-bar replay.

Shared by live tracking (store bars), late-logging replay, restart catch-up and backtests.
"""
from __future__ import annotations

import numpy as np

from core.clock import TF_MS
from data.bars import BarArrays
from exits.engine import ExitEvent, ExitState, on_bar_15m, on_prices
from features import indicators as ind


def last_swing_low(l: np.ndarray, n: int) -> float | None:
    """Most recent CONFIRMED pivot low (lowest of n bars each side; n bars must follow it)."""
    for i in range(len(l) - 1 - n, n - 1, -1):
        v = l[i]
        if v < l[i - n:i].min() and v <= l[i + 1:i + n + 1].min():
            return float(v)
    return None


def ctx_15m(b15: BarArrays, cfg) -> tuple[float, float | None, float | None, float | None]:
    """(close, EMA20, ATR14, last swing low) of 15m bars already cut to the as-of time.
    Uses the same window the live ring buffer holds, so live and backtest values are identical."""
    b15 = b15.tail(cfg.bars.keep["15m"])
    if not len(b15):
        return float("nan"), None, None, None
    e = ind.ema(b15.c, cfg.features.ema_fast)
    a = ind.atr(b15.h, b15.l, b15.c, cfg.universe.atr_period)
    ema = float(e[-1]) if np.isfinite(e[-1]) else None
    atr = float(a[-1]) if np.isfinite(a[-1]) else None
    return float(b15.c[-1]), ema, atr, last_swing_low(b15.l, cfg.exits.swing_pivot_bars_15m)


def replay(st: ExitState, b5: BarArrays, b15: BarArrays, cfg, until_ms: int,
           mark5: BarArrays | None = None, on_bar=None) -> list[ExitEvent]:
    """Advance the state over closed 5m bars after st.last_ts up to until_ms.

    Stop triggers use mark lows when `mark5` is given and config says mark (else last lows).
    Liquidation always checks mark (falls back to last). `on_bar(bar_close_ms, lo, hi)` lets
    the caller watch the same path (e.g. your logged stop).
    """
    use_mark = cfg.trade.stop_trigger_price == "mark" and mark5 is not None and len(mark5)
    mark_lo = dict(zip(mark5.tc.tolist(), mark5.l.tolist())) if mark5 is not None and len(mark5) else {}
    out: list[ExitEvent] = []
    start = st.last_ts
    for i in range(len(b5)):
        tc = int(b5.tc[i])
        if tc <= start or tc <= st.p.entry_ms:
            continue
        if tc > until_ms or st.closed:
            break
        # a bar that opened before entry only counts from entry on: use its close as the only price
        pre_entry = int(b5.t[i]) < st.p.entry_ms
        lo, hi, op = (float(b5.c[i]),) * 3 if pre_entry else (float(b5.l[i]), float(b5.h[i]), float(b5.o[i]))
        mlo = mark_lo.get(tc)
        trig = (mlo if (use_mark and mlo is not None) else lo)
        if on_bar:
            on_bar(tc, trig, hi)
        out += on_prices(st, tc, trig, hi, float(b5.c[i]), mark_low=mlo if mlo is not None else lo,
                         open_price=None if pre_entry else op)
        if not st.closed and tc % TF_MS["15m"] == 0:
            c15 = b15.upto(tc)
            if len(c15) and int(c15.tc[-1]) == tc:
                out += on_bar_15m(st, tc, *ctx_15m(c15, cfg))
    return out
