"""One row per coin per bar: what the scanner saw, in a fixed column layout shared by the live
recorder (research.recorder) and the historical builder (research.history), so the miner
(research.mine) reads both the same way.

Groups of columns:
- meta:     time, coin, scanner state / setup / score, whether a signal fired and was sent
- market:   regime, BTC moves, breadth
- bar:      the 5m bar itself (o/h/l/c/quote volume)
- features: the scanner's features, as unit-free numbers (% distances, ratios)
- score:    the score components
- conds:    every Ignition condition: 1 pass, 0 fail, -1 no data (switched-off ones included)
- live:     things only the live scanner can see (order book, premium, raw OI...) - empty in history
- labels:   what happened NEXT (filled by research.labels, never known at the time)
"""
from __future__ import annotations

import math

IGN_KEYS = ["ignition.break", "ignition.rvol_5m", "ignition.rvol_15m", "ignition.vwap", "ignition.ema",
            "ignition.ret_1h", "ignition.ret_24h", "ignition.rs_1h", "ignition.oi", "ignition.taker", "ignition.cvd",
            "ignition.funding", "ignition.headroom", "ignition.close_pos", "ignition.candle_size"]
SCORE_KEYS = ["rvol", "rs", "oi", "flow", "headroom", "regime", "funding", "wick"]

META = ["ts", "symbol", "state", "setup", "score", "n_pass", "n_conds", "signal", "sent", "suppressed", "regime",
        "config"]
MARKET = ["btc_ret_1h", "btc_ret_45m", "breadth", "btc_above_ema50", "btc_ema50_rising"]
BAR = ["o", "h", "l", "c", "qv"]
FEATURES = ["ret_1h", "ret_4h", "ret_24h", "rs_1h", "rvol_5m", "rvol_15m", "vwap_dist", "ema_gap_15m", "ema50_1h_dist",
            "ema50_1h_rising", "atr_15m_pct", "atr_1h_pct", "bbw_pct_1h", "taker_15m", "cvd_norm", "wicks_dn",
            "wicks_up", "oi_15m", "oi_1h", "oi_4h", "funding_8h", "headroom", "price_discovery", "break_pct",
            "close_pos", "candle_atr", "hour_utc", "weekday"]
SCORES = [f"sc_{k}" for k in SCORE_KEYS]
CONDS = ["c_" + k.split(".", 1)[1] for k in IGN_KEYS]
LIVE = ["premium_pct", "funding_fc_8h", "funding_last_8h", "oi_raw", "spread_pct", "depth_usd", "qv24h",
        "listing_age_d", "tape_age_s"]
LABELS = ["fwd_1h", "fwd_4h", "fwd_24h", "mfe_4h", "mae_4h", "tp3_first"]
COLUMNS = META + MARKET + BAR + FEATURES + SCORES + CONDS + LIVE
TEXT = {"symbol", "state", "setup", "suppressed", "regime", "config"}


def _num(x):
    if x is None:
        return math.nan
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v if math.isfinite(v) else math.nan


def _pct(a, b):
    a, b = _num(a), _num(b)
    return (a / b - 1) * 100 if b and not math.isnan(a) and not math.isnan(b) else math.nan


def build_row(T: int, f, row, b5, regime, config_hash: str, live: dict | None = None,
              signal: dict | None = None, range_bars: int = 144) -> dict:
    """f = Features, row = ScanRow (or None), b5 = 5m BarArrays up to T (last bar closes at T)."""
    from datetime import datetime, timezone
    o, h, l, c = (float(b5.o[-1]), float(b5.h[-1]), float(b5.l[-1]), float(b5.c[-1])) if len(b5) else (math.nan,) * 4
    prior_high = float(b5.h[-1 - range_bars:-1].max()) if len(b5) > range_bars else math.nan
    rng = h - l
    dt = datetime.fromtimestamp(T / 1000, tz=timezone.utc)
    d = {
        "ts": T, "symbol": f.symbol, "state": row.state if row else "", "setup": row.setup if row else "",
        "score": row.score if row else math.nan, "n_pass": row.n_pass if row else math.nan,
        "n_conds": row.n_conds if row else math.nan, "signal": 1 if signal else 0,
        "sent": 1 if signal and not signal.get("suppressed_reason") else 0,
        "suppressed": (signal or {}).get("suppressed_reason") or "", "regime": regime.state if regime else "",
        "config": config_hash,
        "btc_ret_1h": _num(regime.btc_ret_1h) if regime else math.nan,
        "btc_ret_45m": _num(regime.btc_ret_15m_x3) if regime else math.nan,
        "breadth": _num(regime.breadth) if regime else math.nan,
        "btc_above_ema50": _num(regime.btc_above_ema50) if regime else math.nan,
        "btc_ema50_rising": _num(regime.btc_ema50_rising) if regime else math.nan,
        "o": o, "h": h, "l": l, "c": c, "qv": float(b5.qv[-1]) if len(b5) else math.nan,
        "ret_1h": _num(f.ret_1h), "ret_4h": _num(f.ret_4h), "ret_24h": _num(f.ret_24h), "rs_1h": _num(f.rs_1h),
        "rvol_5m": _num(f.rvol_5m), "rvol_15m": _num(f.rvol_15m), "vwap_dist": _pct(f.price, f.vwap_24h),
        "ema_gap_15m": _pct(f.ema20_15m, f.ema50_15m), "ema50_1h_dist": _pct(f.close_1h, f.ema50_1h),
        "ema50_1h_rising": _num(f.ema50_1h_rising), "atr_15m_pct": _num(f.atr_15m) / f.price * 100 if f.price else math.nan,
        "atr_1h_pct": _num(f.atr_1h_pct), "bbw_pct_1h": _num(f.bbw_pct_1h), "taker_15m": _num(f.taker_buy_ratio_15m),
        "cvd_norm": _num(f.cvd_slope_1h_norm), "wicks_dn": _num(f.deep_wicks_24h),
        "wicks_up": _num(getattr(f, "deep_upper_wicks_24h", math.nan)), "oi_15m": _num(f.oi_chg_15m),
        "oi_1h": _num(f.oi_chg_1h), "oi_4h": _num(f.oi_chg_4h),
        "funding_8h": _num(f.funding_8h) * 100 if f.funding_8h is not None else math.nan,
        "headroom": _num(row.headroom_pct) if row else math.nan,
        "price_discovery": _num(row.price_discovery) if row else math.nan,
        "break_pct": _pct(c, prior_high), "close_pos": (c - l) / rng if rng > 0 else math.nan,
        "candle_atr": rng / f.atr_15m if _num(f.atr_15m) > 0 else math.nan,
        "hour_utc": dt.hour, "weekday": dt.weekday(),
    }
    bd = (row.breakdown if row else {}) or {}
    for k in SCORE_KEYS:
        d[f"sc_{k}"] = _num(bd.get(k))
    conds = {c_["key"]: c_ for c_ in (row.ignition_conds if row else [])}
    for k, col in zip(IGN_KEYS, CONDS):
        cc = conds.get(k)
        d[col] = math.nan if cc is None else (-1 if cc["passed"] is None else 1 if cc["passed"] else 0)
    live = live or {}
    for k in LIVE:
        d[k] = _num(live.get(k))
    return d


def fmt(v) -> str:
    """Compact CSV text: 6 significant digits, empty for missing."""
    if isinstance(v, str):
        return v
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer() and abs(v) < 1e15):
        return str(int(v))
    return f"{v:.6g}"
