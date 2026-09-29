"""Market regime (spec §4). Pure function over closed bars; shared by live and backtest."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from data.bars import BarArrays
from features import indicators as ind

RISK_ON, RISK_OFF, NEUTRAL = "RISK_ON", "RISK_OFF", "NEUTRAL"


@dataclass
class Regime:
    state: str
    btc_price: float
    btc_ret_1h: float
    btc_above_ema50: bool
    btc_ema50_rising: bool
    btc_ret_15m_x3: float
    btc_dump: bool
    breadth: float          # % of universe with 1h close > EMA20(1h)
    breadth_n: int

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def breadth_pct(universe_1h: list[BarArrays], ema_period: int) -> tuple[float, int]:
    above = n = 0
    for b in universe_1h:
        if len(b) < ema_period:
            continue
        e = ind.ema(b.c, ema_period)[-1]
        if np.isfinite(e):
            n += 1
            above += int(b.c[-1] > e)
    return (above / n * 100.0 if n else float("nan")), n


def compute_regime(btc5: BarArrays, btc15: BarArrays, btc1h: BarArrays,
                   breadth: float, breadth_n: int, cfg) -> Regime:
    r = cfg.regime
    e50 = ind.ema(btc1h.c, r.ema_period_1h)
    above = bool(np.isfinite(e50[-1]) and btc1h.c[-1] > e50[-1])
    k = r.ema_slope_bars_1h
    rising = bool(len(e50) > k and np.isfinite(e50[-1 - k]) and e50[-1] > e50[-1 - k])
    ret45 = ind.pct_change(btc15.c, r.btc_dump_bars_15m)
    dump = bool(np.isfinite(ret45) and ret45 < r.btc_dump_pct)

    have_breadth = np.isfinite(breadth)
    if dump or (not above and have_breadth and breadth < r.risk_off_breadth):
        state = RISK_OFF
    elif above and have_breadth and breadth >= r.risk_on_breadth:
        state = RISK_ON
    else:
        state = NEUTRAL

    return Regime(state=state, btc_price=float(btc5.c[-1]), btc_ret_1h=ind.pct_change(btc5.c, 12),
                  btc_above_ema50=above, btc_ema50_rising=rising, btc_ret_15m_x3=ret45, btc_dump=dump,
                  breadth=breadth, breadth_n=breadth_n)
