"""Setup A (Ignition) and Setup B (Coil -> Breakout), spec §6.

Every condition is recorded (name, pass/fail/no-data, value) so alerts and the dashboard
can show exactly why something fired or didn't. A condition belongs to a group; groups
listed in signals.disabled_conditions (e.g. "oi" in a backtest without OI history) are
ignored. Missing data (None) counts as a FAIL otherwise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from data.bars import BarArrays
from features.compute import Features
from levels.resistance import Headroom

IGNITION, COIL = "IGNITION", "COIL"


@dataclass
class Cond:
    name: str
    group: str
    passed: bool | None      # None = data unavailable
    value: str


@dataclass
class SetupEval:
    setup: str
    conds: list[Cond]
    hard_pass: bool
    structural_stop: float | None
    trigger_close: float
    extra: dict = field(default_factory=dict)

    @property
    def n_pass(self) -> int:
        return sum(1 for c in self.conds if c.passed)


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _cond(name: str, group: str, value, test, fmt: str = "{:.4g}") -> Cond:
    if not _ok(value):
        return Cond(name, group, None, "n/a")
    shown = fmt.format(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else (
        "yes" if value is True else "no" if value is False else str(value))
    return Cond(name, group, bool(test(value)), shown)


def _pct_diff(a, b):
    """% distance of a above b (None if unavailable)."""
    return (a / b - 1) * 100 if _ok(a) and _ok(b) and b else None


PCT = "{:+.2f}%"


def _hard_pass(conds: list[Cond], disabled: set[str]) -> bool:
    return all(c.passed for c in conds if c.group not in disabled)


# ---- Setup A: Ignition ---------------------------------------------------------

def eval_ignition(f: Features, b5: BarArrays, hr: Headroom, cfg, disabled: set[str]) -> SetupEval:
    g = cfg.ignition
    n = g.range_bars_5m
    o, h, l, c = float(b5.o[-1]), float(b5.h[-1]), float(b5.l[-1]), float(b5.c[-1])
    prior_high = float(b5.h[-1 - n:-1].max()) if len(b5) > n else float("nan")
    rng = h - l
    close_pos = (c - l) / rng if rng > 0 else 0.0
    fund_pct = f.funding_8h * 100 if f.funding_8h is not None else None
    conds = [
        _cond("close > 12h high", "price", _pct_diff(c, prior_high), lambda d: d > 0, PCT),
        _cond("rvol_5m >= %g" % g.min_rvol_5m, "volume", f.rvol_5m, lambda x: x >= g.min_rvol_5m, "{:.1f}x"),
        _cond("rvol_15m >= %g" % g.min_rvol_15m, "volume", f.rvol_15m, lambda x: x >= g.min_rvol_15m, "{:.1f}x"),
        _cond("close > VWAP24h", "trend", _pct_diff(c, f.vwap_24h), lambda d: d > 0, PCT),
        _cond("EMA20 > EMA50 (15m)", "trend", _pct_diff(f.ema20_15m, f.ema50_15m), lambda d: d > 0, PCT),
        _cond("%g%% <= ret_1h <= %g%%" % (g.min_ret_1h, g.max_ret_1h), "momentum", f.ret_1h,
              lambda x: g.min_ret_1h <= x <= g.max_ret_1h, PCT),
        _cond("ret_24h <= %g%%" % g.max_ret_24h, "momentum", f.ret_24h, lambda x: x <= g.max_ret_24h, PCT),
        _cond("rs_1h >= %g%%" % g.min_rs_1h, "momentum", f.rs_1h, lambda x: x >= g.min_rs_1h, PCT),
        _cond("oi_chg_1h >= %g%%" % g.min_oi_chg_1h, "oi", f.oi_chg_1h, lambda x: x >= g.min_oi_chg_1h, PCT),
        _cond("taker_ratio_15m >= %g" % g.min_taker_ratio_15m, "flow", f.taker_buy_ratio_15m,
              lambda x: x >= g.min_taker_ratio_15m, "{:.2f}"),
        # normalised slope (per mean 5m volume) has the same sign as the raw slope and is readable
        _cond("cvd_slope_1h > 0", "flow", f.cvd_slope_1h_norm if _ok(f.cvd_slope_1h_norm) else f.cvd_slope_1h,
              lambda x: x > g.min_cvd_slope, "{:+.2f}"),
        _cond("funding_8h <= %g%%" % g.max_funding_8h_pct, "funding", fund_pct,
              lambda x: x <= g.max_funding_8h_pct, "{:+.4f}%"),
        _cond("headroom >= %g%%" % g.min_headroom_pct, "headroom", hr.pct, lambda x: x >= g.min_headroom_pct,
              "{:.2f}%" + (" (discovery)" if hr.price_discovery else "")),
        _cond("close in top %g%% of candle" % g.close_top_pct, "candle", close_pos,
              lambda x: x >= 1 - g.close_top_pct / 100, "{:.0%} of range"),
        _cond("candle <= %gx ATR15m" % g.max_candle_atr15, "candle",
              rng / f.atr_15m if _ok(f.atr_15m) and f.atr_15m > 0 else None, lambda x: x <= g.max_candle_atr15,
              "{:.2f}x ATR"),
    ]
    stop = l - cfg.plan.ignition_stop_atr15 * f.atr_15m if _ok(f.atr_15m) else None
    return SetupEval(IGNITION, conds, _hard_pass(conds, disabled) and f.warm, stop, c,
                     {"breakout_bar": {"t": int(b5.t[-1]), "o": o, "h": h, "l": l, "c": c},
                      "prior_12h_high": prior_high})


# ---- Setup B: Coil -> Breakout --------------------------------------------------

@dataclass
class CoilWatch:
    symbol: str
    since_ms: int
    last_true_ms: int
    box_high: float
    box_low: float


class CoilTracker:
    """WATCH state per symbol. A watch stays valid for watch_valid_h after the WATCH
    conditions last held; the coil box is re-measured every time they hold."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.watches: dict[str, CoilWatch] = {}

    def watch_conditions(self, f: Features, cfg, disabled: set[str]) -> tuple[list[Cond], bool]:
        k = cfg.coil
        fund_pct = f.funding_8h * 100 if f.funding_8h is not None else None
        conds = [
            _cond("BBW(1h) pct <= %g" % k.max_bbw_pct, "squeeze", f.bbw_pct_1h, lambda x: x <= k.max_bbw_pct,
                  "{:.0f}"),
            _cond("1h close > EMA50(1h)", "trend", _pct_diff(f.close_1h, f.ema50_1h), lambda d: d > 0, PCT),
            _cond("EMA50(1h) rising", "trend", f.ema50_1h_rising, lambda x: x),
            _cond("oi_chg_4h >= %g%%" % k.min_oi_chg_4h, "oi", f.oi_chg_4h, lambda x: x >= k.min_oi_chg_4h, PCT),
            _cond("|ret_4h| <= %g%%" % k.max_abs_ret_4h, "momentum", f.ret_4h, lambda x: abs(x) <= k.max_abs_ret_4h,
                  PCT),
            _cond("funding_8h <= %g%%" % k.max_funding_8h_pct, "funding", fund_pct,
                  lambda x: x <= k.max_funding_8h_pct, "{:+.4f}%"),
        ]
        return conds, _hard_pass(conds, disabled) and f.warm

    def update(self, sym: str, as_of: int, f: Features, b1h: BarArrays, disabled: set[str]) -> tuple[list[Cond], bool]:
        """Update WATCH state. Returns (conditions, is_new_watch)."""
        k = self.cfg.coil
        conds, ok = self.watch_conditions(f, self.cfg, disabled)
        w = self.watches.get(sym)
        if w and as_of - w.last_true_ms > k.watch_valid_h * 3_600_000:
            del self.watches[sym]
            w = None
        new = False
        if ok and len(b1h) >= k.box_bars_1h:
            hi = float(b1h.h[-k.box_bars_1h:].max())
            lo = float(b1h.l[-k.box_bars_1h:].min())
            if w is None:
                self.watches[sym] = CoilWatch(sym, as_of, as_of, hi, lo)
                new = True
            else:
                w.last_true_ms, w.box_high, w.box_low = as_of, hi, lo
        return conds, new

    def eval_entry(self, sym: str, f: Features, b15: BarArrays, hr: Headroom, disabled: set[str]) -> SetupEval | None:
        w = self.watches.get(sym)
        if w is None or not len(b15):
            return None
        k = self.cfg.coil
        c15 = float(b15.c[-1])
        conds = [
            _cond("15m close > coil high", "price", _pct_diff(c15, w.box_high), lambda d: d > 0, PCT),
            _cond("rvol_15m >= %g" % k.min_rvol_15m, "volume", f.rvol_15m, lambda x: x >= k.min_rvol_15m, "{:.1f}x"),
            _cond("taker_ratio_15m >= %g" % k.min_taker_ratio_15m, "flow", f.taker_buy_ratio_15m,
                  lambda x: x >= k.min_taker_ratio_15m, "{:.2f}"),
            _cond("headroom >= %g%%" % k.min_headroom_pct, "headroom", hr.pct, lambda x: x >= k.min_headroom_pct,
                  "{:.2f}%" + (" (discovery)" if hr.price_discovery else "")),
        ]
        stop = w.box_high - self.cfg.plan.coil_stop_atr15 * f.atr_15m if _ok(f.atr_15m) else None
        return SetupEval(COIL, conds, _hard_pass(conds, disabled), stop, c15,
                         {"box_high": w.box_high, "box_low": w.box_low, "watch_since": w.since_ms})

    def consume(self, sym: str) -> None:
        self.watches.pop(sym, None)
