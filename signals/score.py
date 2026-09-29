"""Score 0-100 (spec §6): weighted components + wick-risk penalty. Weights in config."""
from __future__ import annotations

import math

from features.compute import Features
from levels.resistance import Headroom
from signals.setups import IGNITION

# score component -> condition group that disables it
COMPONENT_GROUP = {"oi": "oi", "flow": "flow", "funding": "funding"}


def ramp(x: float | None, lo: float, hi: float) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def compute_score(setup: str, f: Features, regime_state: str, hr: Headroom, cfg,
                  disabled: set[str]) -> tuple[int, dict[str, float]]:
    s = cfg.score
    ign = setup == IGNITION
    raw: dict[str, float] = {}

    rv = s.rvol
    raw["rvol"] = ramp(f.rvol_5m, rv.lo_ignition, rv.hi_ignition) if ign else ramp(f.rvol_15m, rv.lo_coil, rv.hi_coil)
    raw["rs"] = ramp(f.rs_1h, s.rs.lo, s.rs.hi)
    raw["oi"] = ramp(f.oi_chg_1h if ign else f.oi_chg_4h, s.oi.lo, s.oi.hi)
    fl = s.flow
    cvd_ok = 1.0 if (f.cvd_slope_1h is not None and not math.isnan(f.cvd_slope_1h) and f.cvd_slope_1h > 0) else 0.0
    raw["flow"] = (1 - fl.cvd_share) * ramp(f.taker_buy_ratio_15m, fl.taker_lo, fl.taker_hi) + fl.cvd_share * cvd_ok
    raw["headroom"] = ramp(hr.pct, s.headroom.lo, s.headroom.hi)
    raw["regime"] = float(s.regime.get(regime_state, 0.0))
    if f.funding_8h is None:
        raw["funding"] = 0.0
    else:
        raw["funding"] = 1.0 - ramp(f.funding_8h * 100, s.funding.best_pct, s.funding.worst_pct)

    active = {k: v for k, v in raw.items() if COMPONENT_GROUP.get(k) not in disabled}
    total_w = sum(s[k].weight for k in active)
    scale = 100.0 / total_w if total_w else 0.0
    breakdown = {k: round(v * s[k].weight * scale, 1) for k, v in active.items()}
    penalty = min(s.wick_penalty_max, s.wick_penalty_per_bar * max(f.deep_wicks_24h, 0))
    breakdown["wick"] = -float(penalty)
    total = sum(breakdown.values())
    return int(round(min(100.0, max(0.0, total)))), breakdown
