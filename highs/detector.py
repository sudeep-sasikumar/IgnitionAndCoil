"""52-week-high / all-time-high breaks (pure functions, no I/O).

ATH: CoinGecko's own record (`ath`, `ath_date` in /coins/markets, computed from the coin's full
history). A break = CoinGecko reports a higher ATH than we last saw.

52-week high: our per-coin price history - a year of CoinGecko OHLC candles (4-day candles,
their HIGHS) plus one entry per UTC day of our own observations (max of the current price and
CoinGecko's high_24h). A break = price trades above the highest high of the last 365 days,
measured before this observation.

Either break counts only if the old high is at least `min_high_age_days` old: a fresh breakout,
not a trend printing a new high every day. An ATH break is reported as ATH only (it is also a
52-week high by definition).
"""
from __future__ import annotations

from dataclasses import dataclass, field

DAY = 86_400_000
YEAR = 365 * DAY
KEEP = 400 * DAY            # history kept per coin
ATH, H52 = "ATH", "52W"


@dataclass
class CoinState:
    cg_id: str
    symbol: str
    name: str
    ath: float | None = None
    ath_ms: int | None = None
    hist: list[list[float]] = field(default_factory=list)   # [[ts_ms, high], ...] ascending
    hist_ok: bool = False                                    # a year of candles loaded
    hist_ms: int = 0                                         # when the candles were loaded


@dataclass
class Obs:
    price: float
    high_24h: float | None
    ath: float | None
    ath_ms: int | None


@dataclass
class Break:
    kind: str                 # ATH | 52W
    level: float              # the new high (price / 24h high / new ATH)
    prev_high: float
    prev_high_ms: int | None


def high_52w(hist: list[list[float]], now_ms: int) -> tuple[float, int] | None:
    """Highest high of the last 365 days and when it was set."""
    best = None
    for ts, hi in hist:
        if ts >= now_ms - YEAR and (best is None or hi > best[0]):
            best = (float(hi), int(ts))
    return best


def history_days(hist: list[list[float]], now_ms: int) -> float:
    return (now_ms - hist[0][0]) / DAY if hist else 0.0


def check(st: CoinState, ob: Obs, now_ms: int, min_age_days: float, min_hist_days: float) -> list[Break]:
    """Breaks caused by this observation (state not yet updated)."""
    min_age = min_age_days * DAY
    if st.ath is not None and ob.ath is not None and ob.ath > st.ath and (
            st.ath_ms is None or ob.ath_ms is None or ob.ath_ms > st.ath_ms):
        # a new ATH: a break only if the previous ATH was old; either way it is not also a 52W break
        if st.ath_ms is None or now_ms - st.ath_ms >= min_age:
            return [Break(ATH, ob.ath, st.ath, st.ath_ms)]
        return []
    if not st.hist_ok or history_days(st.hist, now_ms) < min_hist_days:
        return []
    h = high_52w(st.hist, now_ms)
    level = max(ob.price, ob.high_24h or 0.0)
    if h and level > h[0] and now_ms - h[1] >= min_age:
        return [Break(H52, level, h[0], h[1])]
    return []


def update(st: CoinState, ob: Obs, now_ms: int) -> bool:
    """Fold the observation into the state. Returns True if the price history changed."""
    if ob.ath is not None:
        st.ath, st.ath_ms = ob.ath, ob.ath_ms
    day = now_ms - now_ms % DAY
    level = max(ob.price, ob.high_24h or 0.0)
    changed = False
    if st.hist and st.hist[-1][0] == day:
        if level > st.hist[-1][1]:
            st.hist[-1][1] = level
            changed = True
    else:
        st.hist.append([day, level])
        changed = True
    if st.hist and st.hist[0][0] < now_ms - KEEP:
        st.hist = [x for x in st.hist if x[0] >= now_ms - KEEP]
        changed = True
    return changed


def merge_candles(st: CoinState, candles: list[list[float]], now_ms: int) -> None:
    """Merge CoinGecko OHLC rows [ts, o, h, l, c] into the history (max per timestamp)."""
    by_ts = {int(ts): float(hi) for ts, hi in st.hist}
    for row in candles:
        ts, hi = int(row[0]), float(row[2])
        by_ts[ts] = max(hi, by_ts.get(ts, hi))
    st.hist = [[ts, hi] for ts, hi in sorted(by_ts.items()) if ts >= now_ms - KEEP]
    st.hist_ok, st.hist_ms = True, now_ms


def excluded(name: str, symbol: str, stable_bases: set[str], keywords: list[str]) -> bool:
    """Stablecoins and wrapped / bridged / staked copies of other coins."""
    n = name.lower()
    return symbol.upper() in stable_bases or any(k.lower() in n for k in keywords)
