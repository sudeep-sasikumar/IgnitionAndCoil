"""Universe filters (incl. wick risk), regime states, OI changes, funding, trade-tape check."""
import numpy as np
import pytest

from data.bars import BarArrays
from data.market import Premium
from data.oi import oi_change_pct
from data.orderflow import TradeTape
from data.universe import Candidate, apply_checks, base_filters, count_deep_wicks, spread_and_bid_depth
from exchange.models import Bar, Trade
from signals.regime import NEUTRAL, RISK_OFF, RISK_ON, breadth_pct, compute_regime


def mk_bars(closes, step=300_000, lows=None, opens=None, vol=1.0):
    bars = []
    for i, c in enumerate(closes):
        o = opens[i] if opens is not None else c
        lo = lows[i] if lows is not None else min(o, c) * 0.999
        bars.append(Bar(i * step, o, max(o, c) * 1.001, lo, c, vol, vol * c, vol / 2, vol * c / 2, 1, (i + 1) * step))
    return BarArrays.from_bars(bars)


# ---- universe -----------------------------------------------------------

def test_spread_and_depth():
    bids = [["99.9", "100"], ["99.6", "50"], ["99.0", "1000"]]
    asks = [["100.1", "10"]]
    spread, depth = spread_and_bid_depth(bids, asks, 0.5)
    assert spread == pytest.approx(0.2)
    assert depth == pytest.approx(99.9 * 100 + 99.6 * 50)     # 99.0 is outside 0.5% of mid 100


def test_wick_risk_counts_deep_lower_wicks_only():
    n = 10
    closes = [100.0] * n
    lows = [99.95] * n
    lows[2] = 98.7   # 1.3% wick -> deep
    lows[5] = 98.8   # 1.2% -> deep (>=)
    lows[7] = 99.0   # 1.0% -> not deep
    b = mk_bars(closes, lows=lows, opens=closes)
    assert count_deep_wicks(b, 288, 1.2) == 2
    assert count_deep_wicks(b, 4, 1.2) == 0                   # only last 4 bars considered


def test_base_filters(cfg):
    info = [
        {"symbol": "AAAUSDT", "baseAsset": "AAA", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "TSLAUSDT", "baseAsset": "TSLA", "quoteAsset": "USDT", "contractType": "TRADIFI_PERPETUAL"},
        {"symbol": "LOWUSDT", "baseAsset": "LOW", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
        {"symbol": "BIGUSDT", "baseAsset": "BIG", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
    ]
    vols = {"AAAUSDT": 6e6, "USDCUSDT": 9e9, "TSLAUSDT": 9e9, "LOWUSDT": 1e6, "BIGUSDT": 7e7}
    cands, rej = base_filters(info, vols, cfg)
    assert [c.symbol for c in cands] == ["BIGUSDT", "AAAUSDT"]  # ranked by volume
    assert rej == {"USDCUSDT": "stablecoin", "LOWUSDT": "volume"}


def test_apply_checks_thresholds(cfg):
    good = Candidate("X", "X", 1e7, spread_pct=0.05, bid_depth_usd=25_000, listing_age_days=30,
                     atr_1h_pct=1.5, deep_wicks=3)
    apply_checks(good, cfg)
    assert good.ok
    bad = Candidate("Y", "Y", 1e7, spread_pct=0.09, bid_depth_usd=19_000, listing_age_days=6,
                    atr_1h_pct=0.9, deep_wicks=4)
    apply_checks(bad, cfg)
    assert len(bad.reasons) == 5


# ---- regime -------------------------------------------------------------

def _btc(trend: float, n1h=120, dump=False):
    c1h = 100 * (1 + trend) ** np.arange(n1h)
    b1h = mk_bars(c1h, step=3_600_000)
    last = c1h[-1]
    c15 = [last] * 10 + ([last * 0.99, last * 0.985, last * 0.99] if dump else [last] * 3)
    b15 = mk_bars(c15, step=900_000)
    b5 = mk_bars([last] * 20)
    return b5, b15, b1h


def test_regime_states(cfg):
    up = _btc(0.002)
    down = _btc(-0.002)
    assert compute_regime(*up, 60.0, 50, cfg).state == RISK_ON
    assert compute_regime(*up, 50.0, 50, cfg).state == NEUTRAL
    assert compute_regime(*down, 30.0, 50, cfg).state == RISK_OFF
    assert compute_regime(*down, 45.0, 50, cfg).state == NEUTRAL
    dumped = compute_regime(*_btc(0.002, dump=True), 70.0, 50, cfg)
    assert dumped.btc_dump and dumped.state == RISK_OFF


def test_breadth():
    up = mk_bars(list(np.linspace(100, 120, 60)), step=3_600_000)
    down = mk_bars(list(np.linspace(120, 100, 60)), step=3_600_000)
    pct, n = breadth_pct([up, up, down, mk_bars([1.0] * 5)], 20)
    assert n == 3 and pct == pytest.approx(200 / 3)


# ---- OI / funding / tape --------------------------------------------------

def test_oi_change_pct():
    ts = [i * 60_000 for i in range(0, 300)]
    oi = [1000 + i for i in range(300)]
    at = ts[-1]
    assert oi_change_pct(ts, oi, at, 60, 150_000) == pytest.approx((1299 / 1239 - 1) * 100)
    assert oi_change_pct(ts, oi, at, 600, 150_000) is None          # not enough history
    assert oi_change_pct(ts[:100], oi[:100], at, 60, 150_000) is None  # latest snapshot too old


def test_funding_normalised_to_8h():
    p = Premium("X", 1, 1, last_funding=0.0001, forecast_funding=0.00005, next_funding_ms=0, cycle_min=240, ts=0)
    assert p.funding_8h("forecast", 480) == pytest.approx(0.0001)
    assert p.funding_8h("last", 480) == pytest.approx(0.0002)


def test_trade_tape_check(cfg):
    tape = TradeTape(cfg)
    for i in range(10):
        tape.on_trade("S", Trade(f"id{i}", 1000 + i, 1.0, 100, 100.0, taker_buy=i < 7))
    tape.on_trade("S", Trade("id0", 1000, 1.0, 100, 100.0, True))  # duplicate ignored
    tape.observed_since["S"] = 0
    ok_bar = Bar(0, 1, 1, 1, 1, 1000, 1000.0, 700, 700.0, 10, 300_000)
    assert tape.check_bar("S", ok_bar) is None and tape.checks_ok == 1
    flipped = ok_bar._replace(tbqv=300.0)                        # kline says 30% buy, tape 70%
    assert "taker-buy" in tape.check_bar("S", flipped)
    tape.observed_since["S"] = 500                               # started watching mid-bar
    assert tape.check_bar("S", flipped) is None
