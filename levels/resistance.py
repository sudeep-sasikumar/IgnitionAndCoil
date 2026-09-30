"""Resistance map and headroom (spec §5, last bullet).

Levels come from closed bars only:
- swing highs (pivot N bars each side, confirmed) on 1h over 14d and 4h over 60d
- 7-day and 30-day highs (from 1h bars)
- volume-profile high-volume nodes from 1h bars over 14d (bins of 0.25%)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from data.bars import BarArrays


@dataclass
class Level:
    price: float
    kind: str        # swing_1h | swing_4h | high_7d | high_30d | hvn


@dataclass
class LevelMap:
    levels: list[Level] = field(default_factory=list)
    supports: list[Level] = field(default_factory=list)   # short side only (short.enabled)

    def above(self, price: float, ignore_within_pct: float) -> list[Level]:
        floor = price * (1 + ignore_within_pct / 100)
        return sorted((lv for lv in self.levels if lv.price > floor), key=lambda lv: lv.price)

    def nearest_above(self, price: float, ignore_within_pct: float) -> Level | None:
        a = self.above(price, ignore_within_pct)
        return a[0] if a else None

    def nearest_below(self, price: float, ignore_within_pct: float) -> Level | None:
        cap = price * (1 - ignore_within_pct / 100)
        b = [lv for lv in self.supports if lv.price < cap]
        return max(b, key=lambda lv: lv.price) if b else None


@dataclass
class Headroom:
    pct: float
    level: Level | None       # None = PRICE_DISCOVERY
    price_discovery: bool


def swing_highs(h: np.ndarray, n: int, lookback: int) -> list[float]:
    """Confirmed pivot highs: bar i is highest of [i-n, i+n], strictly above the left side.
    Only bars with n closed bars on their right qualify (no lookahead)."""
    start = max(n, len(h) - lookback)
    end = len(h) - n                   # exclusive: bar i needs bars i+1..i+n
    if end <= start:
        return []
    wmax = np.lib.stride_tricks.sliding_window_view(h, n).max(axis=1)   # wmax[j] = max(h[j:j+n])
    i = np.arange(start, end)
    mask = (h[i] > wmax[i - n]) & (h[i] >= wmax[i + 1])
    return [float(v) for v in h[i][mask]]


def volume_nodes(b1h: BarArrays, lookback: int, bin_pct: float, window: int, min_ratio: float) -> list[float]:
    """High-volume nodes: log-spaced bins of bin_pct; each bar's volume spread uniformly over
    the bins its [low, high] range covers. A node is a local max within +-window bins whose
    volume is >= min_ratio x the mean of non-empty bins."""
    h, l, v = b1h.h[-lookback:], b1h.l[-lookback:], b1h.v[-lookback:]
    if len(h) < 10:
        return []
    lo, hi = float(l.min()), float(h.max())
    if lo <= 0 or hi <= lo:
        return []
    step = math.log1p(bin_pct / 100)
    nbins = int(math.ceil(math.log(hi / lo) / step)) + 1
    li = np.floor(np.log(l / lo) / step).astype(int)
    hi_i = np.minimum(np.floor(np.log(h / lo) / step).astype(int), nbins - 1)
    w = v / (hi_i - li + 1)                      # each bar's volume spread evenly over its bins
    diff = np.zeros(nbins + 1)
    np.add.at(diff, li, w)
    np.add.at(diff, hi_i + 1, -w)
    vol = np.cumsum(diff[:-1])
    nz = vol[vol > 0]
    if not len(nz):
        return []
    thresh = min_ratio * nz.mean()
    nodes = []
    for i in range(nbins):
        if vol[i] < thresh:
            continue
        w = vol[max(0, i - window): i + window + 1]
        if vol[i] >= w.max():
            center = lo * math.exp((i + 0.5) * step)
            nodes.append(center)
    return nodes


def build_levels(b1h: BarArrays, b4h: BarArrays, cfg) -> LevelMap:
    c = cfg.levels
    lv: list[Level] = []
    if len(b1h):
        lv += [Level(p, "swing_1h") for p in swing_highs(b1h.h, c.pivot_bars, c.swing_1h_lookback_bars)]
        lv.append(Level(float(b1h.h[-c.high_7d_bars_1h:].max()), "high_7d"))
        lv.append(Level(float(b1h.h[-c.high_30d_bars_1h:].max()), "high_30d"))
        lv += [Level(p, "hvn") for p in volume_nodes(b1h, c.vp_lookback_bars_1h, c.vp_bin_pct,
                                                     c.hvn_window_bins, c.hvn_min_ratio)]
    if len(b4h):
        lv += [Level(p, "swing_4h") for p in swing_highs(b4h.h, c.pivot_bars, c.swing_4h_lookback_bars)]
    sup: list[Level] = []
    short = cfg.get("short")
    if short and short.get("enabled"):
        # mirror image for shorts: swing lows (= swing highs of the negated lows), 7d/30d lows, same HVNs
        if len(b1h):
            sup += [Level(-p, "swing_low_1h") for p in swing_highs(-b1h.l, c.pivot_bars, c.swing_1h_lookback_bars)]
            sup.append(Level(float(b1h.l[-c.high_7d_bars_1h:].min()), "low_7d"))
            sup.append(Level(float(b1h.l[-c.high_30d_bars_1h:].min()), "low_30d"))
            sup += [x for x in lv if x.kind == "hvn"]
        if len(b4h):
            sup += [Level(-p, "swing_low_4h") for p in swing_highs(-b4h.l, c.pivot_bars, c.swing_4h_lookback_bars)]
    return LevelMap(lv, sup)


def headroom_down(levels: LevelMap, price: float, atr_1h_pct: float, cfg) -> Headroom:
    """Short side: room down to the nearest support (price discovery below = no support)."""
    c = cfg.levels
    lv = levels.nearest_below(price, c.ignore_within_pct)
    if lv is None:
        return Headroom(pct=c.discovery_atr_mult * atr_1h_pct, level=None, price_discovery=True)
    return Headroom(pct=(1 - lv.price / price) * 100, level=lv, price_discovery=False)


def headroom(levels: LevelMap, price: float, atr_1h_pct: float, cfg) -> Headroom:
    c = cfg.levels
    lv = levels.nearest_above(price, c.ignore_within_pct)
    if lv is None:
        return Headroom(pct=c.discovery_atr_mult * atr_1h_pct, level=None, price_discovery=True)
    return Headroom(pct=(lv.price / price - 1) * 100, level=lv, price_discovery=False)
