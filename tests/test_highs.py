"""Highs tab: 52-week / all-time-high break detection, the Telegram text and the study's events."""
import numpy as np
import pytest

from highs import store
from highs.detector import (ATH, DAY, H52, CoinState, Obs, check, excluded, high_52w, merge_candles, ohlc_days,
                            peak_after, update)
from highs.service import HighsService, alert_text
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


# ---- peak since the break ----------------------------------------------------------------------

H4 = 4 * 3_600_000


def test_peak_after_counts_only_candles_closed_after_the_break():
    t0 = NOW - 2 * DAY
    candles = [[t0 - H4, 1, 99.0, 1, 1], [t0, 1, 50.0, 1, 1], [t0 + H4, 1, 12.0, 1, 1], [t0 + 2 * H4, 1, 14.5, 1, 1],
               [t0 + 3 * H4, 1, 13.0, 1, 1]]
    assert peak_after(candles, t0) == (14.5, t0 + 2 * H4)            # the candle closing AT the break is before it
    assert peak_after(candles, t0, t0 + H4) == (12.0, t0 + H4)       # tracking window ended
    assert peak_after(candles, t0 + 3 * H4) is None
    assert [ohlc_days(int(d * DAY)) for d in (0.5, 0.99, 5, 6.99, 13, 29, 60, 170, 300)] == [1, 7, 7, 14, 14, 30, 90, 180, 365]


class _Eng:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.clock = type("C", (), {"now_ms": staticmethod(lambda: NOW)})()
        self.universe = type("U", (), {"exchange_symbols": set()})()
        self.premium = type("P", (), {"mark": staticmethod(lambda s: None)})()


def _event(db, ts, level=11.0, price=10.9, **over):
    e = {"cg_id": "x", "symbol": "XYZ", "name": "XYZ Coin", "rank": 5, "kind": H52, "ts": ts, "price": price,
         "level": level, "prev_high": 10.0, "prev_high_ms": ts - 30 * DAY, "market_cap": 1e9, "volume": 1e7,
         "weex_symbol": None, **over}
    return store.add_events(db, [e])[0]


def test_peaks_are_followed_live_and_filled_for_older_records(cfg, tmp_path):
    from data.db import Database
    db = Database(f"sqlite:///{tmp_path / 'h.db'}")
    old = _event(db, NOW - 3 * DAY)                                   # recorded before peaks existed
    new = _event(db, NOW - DAY, level=12.0, price=12.0, peak=12.0, peak_ms=NOW - DAY)
    svc = HighsService(_Eng(cfg, db))
    svc.load()
    assert list(svc.refill) == ["x"]
    # live: the older record waits for its candles; the followed one takes the 24h high
    got = svc.track_peaks("x", Obs(12.5, 13.0, 50.0, NOW - 900 * DAY), NOW, NOW - 600_000)
    assert got == {new: (13.0, NOW)}
    assert svc.track_peaks("x", Obs(12.0, 12.9, 50.0, NOW - 900 * DAY), NOW + 600_000, NOW) == {}     # no new peak
    # candles: each record only counts candles after its own break; a peak already seen is never lowered
    t0 = NOW - 3 * DAY
    candles = [[t0 - H4, 1, 30.0, 1, 1], [t0 + H4, 1, 15.0, 1, 1], [NOW - DAY + H4, 1, 12.2, 1, 1]]
    got = svc.fill_peaks("x", candles)
    assert got == {old: (15.0, t0 + H4)}
    store.set_peaks(db, got)
    rows = {e.id: e for e in store.events_since(db, 0)}
    assert rows[old].peak == 15.0 and rows[old].peak_ms == t0 + H4 and rows[new].peak == 12.0
    # a new all-time high after the break: CoinGecko's exact value and time win
    got = svc.track_peaks("x", Obs(15.5, 15.8, 16.0, NOW + 3_600_000), NOW + 7_200_000, NOW + 600_000)
    assert got == {old: (16.0, NOW + 3_600_000), new: (16.0, NOW + 3_600_000)}
    # not watched for a day: the 24h high cannot cover the gap, so candles are requested again
    svc.refill.clear()
    svc.track_peaks("x", Obs(1.0, 1.0, 16.0, NOW + 3_600_000), NOW + 3 * DAY, NOW + 7_200_000)
    assert list(svc.refill) == ["x"]
    # followed for highs.peak_track_days only
    assert svc.track_peaks("x", Obs(99.0, 99.0, 16.0, NOW + 3_600_000), NOW + 40 * DAY, NOW + 40 * DAY - 600_000) == {}


def test_peak_columns_are_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from data.db import Database
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as con:                                           # the table as created before peaks existed
        con.execute(text("CREATE TABLE highs_events (id INTEGER PRIMARY KEY, cg_id VARCHAR(128), symbol VARCHAR(64), "
                         "name VARCHAR(256), rank INTEGER, kind VARCHAR(8), ts BIGINT, price FLOAT, level FLOAT, "
                         "prev_high FLOAT, prev_high_ms BIGINT, market_cap FLOAT, volume FLOAT, weex_symbol VARCHAR(32), "
                         "alert_status VARCHAR(16))"))
        con.execute(text("INSERT INTO highs_events (cg_id, symbol, name, kind, ts, price, level, prev_high) "
                         "VALUES ('x', 'XYZ', 'XYZ Coin', '52W', 1000, 10.9, 11.0, 10.0)"))
    eng.dispose()
    db = Database(url)
    assert {"peak", "peak_ms"} <= {c["name"] for c in inspect(db.engine).get_columns("highs_events")}
    (e,) = store.peak_events(db, 10 ** 15)                              # old and never filled: still returned
    assert e.peak is None and e.level == 11.0
    Database(url)                                                       # running it again changes nothing


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


# ---- breakout study (intraday) ---------------------------------------------------------------------

def test_breakout_study_breaks_follow_the_live_rules():
    from highs.breakout_study import find_breaks
    n = 800
    d = np.zeros((n, 7))
    d[:, 0] = np.arange(n) * DAY
    d[:, 2] = 10.0                                   # highs
    d[100, 2] = 50.0                                 # all-history high, later more than a year old
    d[600, 2] = 20.0                                 # above a year of 10s (day 100 has left the window): 52W
    d[700, 2] = 21.0                                 # breaks the 52-week high (100 days old): 52W
    d[703, 2] = 22.0                                 # above day 700's high, only 3 days old: a trend, no event
    d[750, 2] = 60.0                                 # above the all-history high (650 days old): AH
    d[752, 2] = 61.0                                 # the all-history high is 2 days old: no event
    assert find_breaks(d, 7) == [(600, "52W", 10.0, 235), (700, "52W", 20.0, 600), (750, "AH", 50.0, 100)]
    d2 = d.copy()
    d2[400:, 0] += 5 * DAY                           # a 5-day gap in the history: the year around it is skipped
    assert [e[0] for e in find_breaks(d2, 7)] == []
    d2[:, 0] = np.arange(n) * DAY
    assert find_breaks(d2[:300], 7) == []            # a pair needs a year of history


def test_breakout_study_measures_and_races():
    from highs.breakout_report import Set
    from highs.breakout_study import DN, H5, UP, measure
    k, n = 30, 30 + 1 + H5["48h"]
    m5 = np.zeros((n, 7))
    m5[:, 0] = np.arange(n) * 300_000
    m5[:, 1:5] = 100.0
    m5[:, 5] = 1.0
    m5[k + 3, 2] = 102.5                             # +2.5% after 3 bars
    m5[k + 10, 3] = 96.5                             # -3.5% after 10 bars
    m5[k + 20, 2] = 111.0                            # +11% after 20 bars (the peak)
    m5[-1, 4] = 101.0
    h1 = np.zeros((0, 7))
    o = measure(m5, h1, k, old=99.0)
    assert o["mfe_48h"] == 11.0 and o["mae_48h"] == -3.5 and o["t_peak48"] == 19 and o["dip_before_peak48"] == -3.5
    assert o["up48"][UP.index(2)] == 2 and o["up48"][UP.index(10)] == 19 and o["up48"][UP.index(15)] == -1
    assert o["dn48"][DN.index(3)] == 9 and o["dn48"][DN.index(4)] == -1 and o["below_old48"] == -1
    assert o["ret_48h"] == 1.0 and o["gap"] == pytest.approx(1.0101, abs=1e-3) and "mfe_30d" not in o
    assert measure(m5[:k + 200], h1, k, old=99.0) is None           # trading stopped inside the 48 hours
    costs = {"win": 0.13, "loss": 0.22}
    s = Set([{"t": 0, "d0": o}], "d0", costs)
    res, code = s.race(2, 3)                         # +2% came first
    assert code[0] == 1 and res[0] == pytest.approx(2 - 0.13 - 0.01 * (3 * 5 / 60) / 8)
    res, code = s.race(10, 3)                        # -3% came before +10%
    assert code[0] == -1 and res[0] == pytest.approx(-3 - 0.22 - 0.01 * (10 * 5 / 60) / 8)
    res, code = s.race(15, 4)                        # neither: closed after 48h at +1%
    assert code[0] == 0 and res[0] == pytest.approx(1 - 0.22 - 0.01 * 48 / 8)
    m5[k + 3, 3] = 96.0                              # target and stop in the same candle: the stop is assumed
    res, code = Set([{"t": 0, "d0": measure(m5, h1, k, old=99.0)}], "d0", costs).race(2, 3)
    assert code[0] == -1


def test_breakout_level_entry_at_the_old_high():
    from highs.breakout_level import measure_level, race
    from highs.breakout_study import DN, UP
    n1, n5 = 1000, 700
    m1 = np.zeros((n1, 7))
    m1[:, 0] = np.arange(n1) * 60_000
    m1[:, 1:5] = 99.0
    m1[2, 1:5] = [99.0, 100.5, 98.5, 100.2]         # minute 2 crosses the old high (100); its low is 1.5% under it
    m1[3:, 1:5] = 100.2
    m1[6, 2] = 103.5                                 # +3.5% four minutes after the cross
    m1[30, 3] = 95.9                                 # -4.1% later
    m5 = np.zeros((n5, 7))
    m5[:, 0] = np.arange(n5) * 300_000
    m5[:, 1:5] = 100.2
    m5[400, 2] = 111.0                               # +11% in the 5-minute part (after the 1,000 minutes)
    m5[-1, 4] = 102.0
    r = measure_level(m1, m5, old=100.0)
    assert r["cross_min"] == 2 and not r["gapped"] and r["fill_vs_old_pct"] == 0.0
    assert r["pess"]["dn"][DN.index(1)] == 1.0 and r["opt"]["dn"][DN.index(1)] == 29.0     # the fill candle's low
    assert r["pess"]["up"][UP.index(3)] == r["opt"]["up"][UP.index(3)] == 5.0
    assert r["opt"]["up"][UP.index(10)] == (400 * 5 + 5) - 2 and r["pess"]["ret48"] == r["opt"]["ret48"]
    assert r["m0"]["vs_old_pct"] == pytest.approx(0.2) and r["m0"]["up"][UP.index(3)] == 4.0
    recs = [{"t": 0, **r}]
    exits = (0.02, 0.11)
    res, code, _ = race(recs, "opt", 3, 1, 0.18, exits)             # +3% (minute 5) before -1% (minute 29)
    assert code[0] == 1 and res[0] == pytest.approx(3 - 0.02 - 0.18 - 0.01 * (5 / 60) / 8)
    res, code, _ = race(recs, "pess", 3, 1, 0.18, exits)            # worst case: the fill candle's low stops it
    assert code[0] == -1 and res[0] == pytest.approx(-1 - 0.11 - 0.18 - 0.01 * (1 / 60) / 8)
    res, code, _ = race(recs, "opt", 10, 4, 0.18, exits)            # -4% (minute 29) before +10%
    assert code[0] == -1
    m1[2, 1] = 100.4                                 # the crossing minute OPENS above the old high: filled at the open
    g = measure_level(m1, m5, old=100.0)
    assert g["gapped"] and g["fill_vs_old_pct"] == pytest.approx(0.4) and g["opt"] == g["pess"]
    assert measure_level(m1[:, :], m5[:300], old=100.0) is None      # no 48 hours of data


# ---- approaching a high --------------------------------------------------------------------------

def test_next_level_is_the_level_check_would_report():
    from highs.detector import next_level
    st = coin_with_history(peak=20.0, peak_age=40)                   # 52-week high 20 (40 days old), ATH 50
    nl = next_level(st, NOW, 7, 300)
    assert (nl.kind, nl.level) == (H52, 20.0)
    (b,) = check(st, Obs(20.1, None, 50.0, st.ath_ms), NOW, 7, 300)  # trading just above it IS that break
    assert (b.kind, b.prev_high) == (nl.kind, nl.level)
    fresh = coin_with_history(peak=20.0, peak_age=3)                 # 52-week high only 3 days old: a trend...
    nl = next_level(fresh, NOW, 7, 300)
    assert (nl.kind, nl.level) == (ATH, 50.0)                        # ...but the old all-time high above it counts
    fresh.ath_ms = NOW - 2 * DAY
    assert next_level(fresh, NOW, 7, 300) is None                    # both fresh: nothing to wait for
    young = coin_with_history(days=100, peak=20.0, peak_age=40)      # too little history for a 52-week high
    assert next_level(young, NOW, 7, 300).kind == ATH
    same = coin_with_history(peak=50.0, peak_age=40)                 # the 52-week high IS the all-time high
    same.ath_ms = NOW - 40 * DAY
    assert next_level(same, NOW, 7, 300).kind == ATH


def test_approaching_list_alerts_once_per_level(cfg, tmp_path):
    from data.db import Database
    from highs.service import approach_text
    eng = _Eng(cfg, Database(f"sqlite:///{tmp_path / 'a.db'}"))
    eng.universe.exchange_symbols = {"XYZUSDT"}
    marks = {"XYZUSDT": 19.6}
    eng.premium = type("P", (), {"mark": staticmethod(lambda s: marks.get(s))})()
    svc = HighsService(eng)
    svc.load()
    st = coin_with_history(peak=20.0, peak_age=40)
    m = {"rank": 7, "market_cap": 1e9, "volume": 5e6}
    assert svc.approach_row(st, 19.0, m, NOW) is None                # 5.3% under the level: not near yet
    assert svc.approach_row(st, 20.5, m, NOW) is None                # already above it
    a = svc.approach_row(st, 19.6, m, NOW)
    assert a["kind"] == H52 and a["level"] == 20.0 and a["dist_pct"] == pytest.approx(2.0408, abs=1e-3)
    assert a["target"] == pytest.approx(22.0) and a["stop"] == pytest.approx(19.2) and a["weex_symbol"] == "XYZUSDT"
    marks["XYZUSDT"] = 30.0                                          # same ticker, another price: another coin
    assert svc.approach_row(st, 19.6, m, NOW)["weex_symbol"] is None
    marks.clear()                                                    # no WEEX price yet: the ticker alone decides
    assert svc.weex_symbol("xyz", 19.6) == "XYZUSDT" and svc.weex_symbol("abc", 1.0) is None
    svc.approaching = [a, {**a, "cg_id": "y", "weex_symbol": None}]
    assert [x["cg_id"] for x in svc.approach_new(NOW)] == ["x"]      # Telegram: WEEX perps only
    svc.approach_sent = {"x": [20.0, NOW]}
    assert svc.approach_new(NOW + 3_600_000) == []                   # the same level: not again within a day
    assert len(svc.approach_new(NOW + 25 * 3_600_000)) == 1          # ...but again after it
    svc.approaching = [{**a, "level": 21.0}]
    assert len(svc.approach_new(NOW + 3_600_000)) == 1               # a new level: alert
    t = approach_text([a], NOW, (10.0, 4.0), "https://x.test", False)
    assert "XYZ #7 $19.600 → 52W high $20.000 (2.0% away, set 40d ago)" in t and "WEEX XYZUSDT" in t
    assert "target +10% $22.000 · stop -4% $19.200" in t and t.endswith("https://x.test/highs")
