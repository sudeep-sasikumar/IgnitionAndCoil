"""Highs tab: 52-week / all-time-high break detection, the Telegram text and the study's events."""
import numpy as np
import pytest

from highs.detector import ATH, DAY, H52, CoinState, Obs, check, excluded, high_52w, merge_candles, update
from highs.service import alert_text
from highs.study import find_events, outcome

NOW = 2_000 * DAY + 12 * 3_600_000


def coin_with_history(days=400, level=10.0, peak=None, peak_age=None):
    st = CoinState("x", "xyz", "XYZ Coin", ath=50.0, ath_ms=NOW - 900 * DAY)
    st.hist = [[NOW - k * DAY - NOW % DAY, level] for k in range(days, 0, -1)]
    if peak is not None:
        st.hist[-peak_age][1] = peak
    st.hist_ok = True
    return st


def test_52w_break_only_when_the_old_high_is_old_enough():
    st = coin_with_history(peak=12.0, peak_age=30)
    brk = check(st, Obs(12.5, 12.6, 50.0, st.ath_ms), NOW, 7, 300)
    assert [b.kind for b in brk] == [H52] and brk[0].prev_high == 12.0 and brk[0].level == 12.6
    fresh = coin_with_history(peak=12.0, peak_age=3)          # a high 3 days ago: trend, not a breakout
    assert check(fresh, Obs(12.5, 12.6, 50.0, fresh.ath_ms), NOW, 7, 300) == []
    assert check(st, Obs(11.9, 11.95, 50.0, st.ath_ms), NOW, 7, 300) == []   # below the high


def test_52w_needs_history_and_ignores_highs_older_than_a_year():
    young = coin_with_history(days=200)
    assert check(young, Obs(20.0, 20.0, 50.0, young.ath_ms), NOW, 7, 300) == []
    st = coin_with_history(days=400)
    st.hist[0][1] = 30.0                                       # 400 days ago: outside the 52-week window
    assert high_52w(st.hist, NOW)[0] == 10.0
    assert [b.kind for b in check(st, Obs(11.0, 11.0, 50.0, st.ath_ms), NOW, 7, 300)] == [H52]


def test_ath_break_is_reported_as_ath_only():
    st = coin_with_history()
    new = check(st, Obs(55.0, 55.0, 55.0, NOW - 60_000), NOW, 7, 300)
    assert [(b.kind, b.prev_high) for b in new] == [(ATH, 50.0)]
    recent = coin_with_history()
    recent.ath_ms = NOW - 2 * DAY                              # ATH two days ago: continuation, no alert
    assert check(recent, Obs(55.0, 55.0, 55.0, NOW - 60_000), NOW, 7, 300) == []
    first = CoinState("y", "y", "Y")                           # first sighting: no baseline, no break
    assert check(first, Obs(5.0, 5.0, 5.0, NOW), NOW, 7, 300) == []


def test_update_keeps_one_entry_per_day_and_the_new_ath():
    st = coin_with_history(days=10)
    update(st, Obs(11.0, 12.0, 60.0, NOW), NOW)
    update(st, Obs(11.5, 11.8, 60.0, NOW), NOW + 60_000)       # same day, lower: unchanged
    assert st.hist[-1] == [NOW - NOW % DAY, 12.0] and st.ath == 60.0
    update(st, Obs(13.0, 13.0, 60.0, NOW), NOW + DAY)
    assert st.hist[-1] == [NOW + DAY - (NOW + DAY) % DAY, 13.0]


def test_merge_candles_uses_highs():
    st = CoinState("x", "x", "X")
    merge_candles(st, [[NOW - 8 * DAY, 1, 5, 0.5, 2], [NOW - 4 * DAY, 2, 7, 1.5, 3]], NOW)
    assert st.hist_ok and [h for _, h in st.hist] == [5.0, 7.0]


def test_exclusions():
    stables = {"USDT", "USDC"}
    assert excluded("Tether", "usdt", stables, ["wrapped"])
    assert excluded("Wrapped Bitcoin", "wbtc", stables, ["wrapped"])
    assert not excluded("Solana", "sol", stables, ["wrapped"])


def test_alert_text():
    e = [{"kind": ATH, "symbol": "SOL", "rank": 6, "price": 245.1, "prev_high": 240.0, "prev_high_ms": NOW - 400 * DAY,
          "volume": 3.2e9, "weex_symbol": "SOLUSDT"},
         {"kind": H52, "symbol": "ARB", "rank": 58, "price": 1.23, "prev_high": 1.2, "prev_high_ms": NOW - 40 * DAY,
          "volume": 1.5e8, "weex_symbol": None}]
    t = alert_text(e, NOW, 2000, "https://x.test", False)
    assert "🚀 ATH SOL #6 $245.100" in t and "old ATH $240.000 (1.1y ago)" in t and "WEEX SOLUSDT" in t
    assert "📈 52W ARB #58" in t and "(40d ago)" in t and t.endswith("https://x.test/highs")


# ---- study -------------------------------------------------------------------------------------

def _daily(highs, closes=None):
    n = len(highs)
    c = np.array(closes if closes is not None else highs, dtype=float)
    h = np.array(highs, dtype=float)
    return np.column_stack([np.arange(n) * DAY, c, h, np.minimum(h, c) * 0.99, c])


def test_study_events_ath_and_52w_without_lookahead():
    # 500 days: ATH 100 on day 50, then a range at 50 with a 60 high on day 200, then breaks
    hi = [10.0] * 500
    hi[50] = 100.0
    for i in range(51, 500):
        hi[i] = 50.0
    hi[200] = 60.0
    closes = list(hi)
    closes[420] = 65.0                    # closes above the 52w high (60, 220 days old) but below the ATH
    hi[420] = 66.0
    closes[480], hi[480] = 120.0, 121.0   # closes above the ATH (100, 430 days old)
    ev = find_events(_daily(hi, closes), 7, ath_valid=True)
    assert [(i, k) for i, k, _, _ in ev] == [(420, "52W"), (480, "ATH")]
    assert ev[0][2] == 60.0 and ev[1][2] == 100.0
    # without a trustworthy ATH on Binance the second break is only a 52-week high
    assert [(i, k) for i, k, _, _ in find_events(_daily(hi, closes), 7, ath_valid=False)] == [(420, "52W"), (480, "52W")]


def test_study_outcome():
    d = _daily([10.0] * 100, [10.0] * 40 + [12.0] * 60)
    o = outcome(d, 40, 11.0, {})
    assert o["ret"][1] == pytest.approx(0.0) and o["held30"] and not o["failed3"]
