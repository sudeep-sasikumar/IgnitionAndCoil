"""Setups, scoring, levels and the signal engine on synthetic data."""
import numpy as np
import pytest

from data.bars import BarArrays
from exchange.models import Bar
from levels.resistance import Headroom, Level, LevelMap, build_levels, headroom, swing_highs, volume_nodes
from signals.engine import ENTRY, SKIP, WATCH, SignalEngine, session_tags
from signals.regime import Regime
from signals.score import compute_score, ramp
from signals.setups import COIL, IGNITION, CoilTracker, eval_ignition
from core.config import load_config
from tests.helpers import M5, breakout_bars, features, flat_bars

C = load_config()   # failing examples are derived from the live thresholds, so tuning never breaks them

H1 = 3_600_000
ROOM = Headroom(pct=6.0, level=Level(108.1, "swing_1h"), price_discovery=False)


def regime(state="RISK_ON"):
    return Regime(state, 50000, 0.2, True, True, 0.1, False, 60.0, 30)


# ---- Ignition -------------------------------------------------------------------

def test_ignition_passes_on_clean_breakout(cfg):
    ev = eval_ignition(features(), breakout_bars(), ROOM, cfg, set())
    failed = [c.name for c in ev.conds if not c.passed]
    assert ev.hard_pass, failed
    assert ev.structural_stop == pytest.approx(100.3 - 0.1 * 0.8)


@pytest.mark.parametrize("override,bars_last,cond", [
    ({}, (100.5, 100.6, 100.3, 100.45), "close > 12h high"),        # no breakout
    ({"rvol_5m": C.ignition.min_rvol_5m - 0.1}, None, "rvol_5m"),
    ({"ret_1h": C.ignition.max_ret_1h + 0.5}, None, "ret_1h"),
    ({"ret_24h": 30.0}, None, "ret_24h"),
    ({"rs_1h": 0.5}, None, "rs_1h"),
    ({"oi_chg_1h": 1.0}, None, "oi_chg_1h"),
    ({"oi_chg_1h": None}, None, "oi_chg_1h"),                         # no OI history -> fail
    ({"taker_buy_ratio_15m": 0.5}, None, "taker_ratio"),
    ({"cvd_slope_1h": -1.0, "cvd_slope_1h_norm": -0.1}, None, "cvd_slope"),
    ({"funding_8h": 0.0004}, None, "funding"),
    ({"vwap_24h": 103.0}, None, "VWAP"),
    ({"ema20_15m": 99.0}, None, "EMA20 > EMA50"),
    ({}, (100.5, 102.9, 100.3, 102.0), "top 30%"),                    # close low in the candle
    ({"atr_15m": 0.5}, None, "ATR15m"),                               # candle 1.9 > 2.5 x 0.5
])
def test_ignition_each_condition_can_fail(cfg, override, bars_last, cond):
    bars = breakout_bars(last=bars_last) if bars_last else breakout_bars()
    ev = eval_ignition(features(**override), bars, ROOM, cfg, set())
    assert not ev.hard_pass
    assert any(cond in c.name for c in ev.conds if not c.passed)


def test_ignition_headroom_and_disabled_groups(cfg):
    low = Headroom(pct=cfg.ignition.min_headroom_pct - 0.5, level=Level(105, "hvn"), price_discovery=False)
    assert not eval_ignition(features(), breakout_bars(), low, cfg, set()).hard_pass
    # backtest without OI history: 'oi' group disabled -> missing OI no longer blocks
    assert eval_ignition(features(oi_chg_1h=None), breakout_bars(), ROOM, cfg, {"oi"}).hard_pass


# ---- Coil ------------------------------------------------------------------------

def coil_features(**o):
    return features(**{"bbw_pct_1h": 8.0, "ret_4h": 0.5, "oi_chg_4h": 4.0, "funding_8h": 0.00005, **o})


def test_coil_watch_entry_and_expiry(cfg):
    tr = CoilTracker(cfg)
    b1h = flat_bars(50, H1, 100.0, 0.5)                 # box = 99.5 .. 100.5
    t0 = 1000 * H1
    _, new = tr.update("C", t0, coil_features(), b1h, set())
    assert new and tr.watches["C"].box_high == pytest.approx(100.5)
    _, new2 = tr.update("C", t0 + M5, coil_features(), b1h, set())
    assert not new2                                      # same watch continues

    b15 = BarArrays.from_bars([Bar(0, 100.4, 101.2, 100.3, 101.0, 1, 100, 0.6, 60, 1, 900_000)])
    ev = tr.eval_entry("C", coil_features(rvol_15m=3.0), b15, ROOM, set())
    assert ev.setup == COIL and ev.hard_pass
    assert ev.structural_stop == pytest.approx(100.5 - 0.25 * 0.8)
    weak = tr.eval_entry("C", coil_features(rvol_15m=C.coil.min_rvol_15m - 0.1), b15, ROOM, set())
    assert not weak.hard_pass

    # conditions stop holding: watch survives 24h after they last held, then expires
    tr.update("C", t0 + 23 * H1, coil_features(bbw_pct_1h=50), b1h, set())
    assert "C" in tr.watches
    tr.update("C", t0 + 25 * H1, coil_features(bbw_pct_1h=50), b1h, set())
    assert "C" not in tr.watches


def test_coil_watch_requires_all_conditions(cfg):
    tr = CoilTracker(cfg)
    b1h = flat_bars(50, H1)
    for bad in ({"bbw_pct_1h": 20}, {"ema50_1h_rising": False}, {"oi_chg_4h": 2.0}, {"ret_4h": 2.0},
                {"funding_8h": 0.0002}, {"close_1h": 98.0}):
        _, new = tr.update("C", 0, coil_features(**bad), b1h, set())
        assert not new, bad


# ---- Score -----------------------------------------------------------------------

def test_score_range_rescale_and_penalty(cfg):
    best = features(rvol_5m=9, rs_1h=5, oi_chg_1h=7, taker_buy_ratio_15m=0.8, funding_8h=-0.0001)
    hi = Headroom(12.0, None, True)
    s, bd = compute_score(IGNITION, best, "RISK_ON", hi, cfg, set())
    assert s == 100 and bd["wick"] == 0
    s_off, _ = compute_score(IGNITION, best, "RISK_OFF", hi, cfg, set())
    assert s_off == 90
    # OI disabled (no history): remaining components rescaled so a perfect setup still scores 100
    s2, bd2 = compute_score(IGNITION, features(**{**best.as_dict(), "oi_chg_1h": None}), "RISK_ON", hi, cfg, {"oi"})
    assert s2 == 100 and "oi" not in bd2
    s3, bd3 = compute_score(IGNITION, features(**{**best.as_dict(), "deep_wicks_24h": 5}), "RISK_ON", hi, cfg, set())
    assert bd3["wick"] == -15 and s3 == 85
    assert ramp(None, 0, 1) == 0 and ramp(0.5, 0, 1) == 0.5


# ---- Levels ----------------------------------------------------------------------

def test_swing_highs_confirmed_only():
    h = np.array([1, 2, 3, 9, 3, 2, 1, 2, 3, 8, 4.0])        # 9 at i=3 confirmed; 8 at i=9 has 1 bar right
    assert swing_highs(h, 3, 100) == [9.0]


def test_volume_node_found_at_cluster():
    bars = []
    for i in range(200):
        p = 100.0 if i % 4 else 110.0                        # most volume traded around 100
        v = 100.0 if p == 100 else 5.0
        bars.append(Bar(i * H1, p, p * 1.001, p * 0.999, p, v, v * p, 0, 0, 1, (i + 1) * H1))
    nodes = volume_nodes(BarArrays.from_bars(bars), 336, 0.25, 3, 1.5)
    assert any(abs(n / 100 - 1) < 0.003 for n in nodes)


def test_headroom_ignores_near_levels_and_discovery(cfg):
    lm = LevelMap([Level(100.2, "hvn"), Level(106.0, "swing_1h"), Level(99.0, "high_7d")])
    hr = headroom(lm, 100.0, 1.5, cfg)
    assert hr.level.price == 106.0 and hr.pct == pytest.approx(6.0)     # 100.2 is within 0.3%
    d = headroom(LevelMap([Level(99, "high_7d")]), 100.0, 1.5, cfg)
    assert d.price_discovery and d.pct == pytest.approx(4.5)             # 3 x ATR1h%


def test_build_levels_contains_highs(cfg):
    lm = build_levels(flat_bars(800, H1), flat_bars(400, 4 * H1), cfg)
    kinds = {lv.kind for lv in lm.levels}
    assert {"high_7d", "high_30d"} <= kinds


# ---- Signal engine ----------------------------------------------------------------

STRONG = dict(rvol_5m=6.0, rs_1h=2.5, oi_chg_1h=4.0, taker_buy_ratio_15m=0.64, bbw_pct_1h=40.0)


def _eval(se, cfg, as_of, f=None, bars=None):
    b5 = bars or breakout_bars()
    b1h = flat_bars(100, H1, 100.0, 0.5)
    b4h = flat_bars(100, 4 * H1, 100.0, 0.5)
    return se.evaluate("TSTUSDT", as_of, f or features(**STRONG), b5, flat_bars(60, 900_000), b1h, b4h, regime(),
                       None, 102.0, 2)


def test_signal_engine_entry_and_cooldown(cfg):
    se = SignalEngine(cfg)
    t = 1_000_000 * M5
    row, sigs, _ = _eval(se, cfg, t)
    assert row.state == ENTRY and len(sigs) == 1
    sig = sigs[0]
    assert sig.setup == IGNITION and sig.score >= cfg.score.min_entry
    assert sig.plan.ref_entry == 102.0 and sig.headroom["price_discovery"]
    # same symbol within 2h: no new ENTRY
    row2, sigs2, _ = _eval(se, cfg, t + 60 * 60_000)
    assert not sigs2 and row2.state == SKIP
    _, sigs3, _ = _eval(se, cfg, t + 121 * 60_000)
    assert len(sigs3) == 1


def test_dry_run_leaves_no_cooldown(cfg):
    se = SignalEngine(cfg)
    t = 1_000_000 * M5
    b5 = breakout_bars()
    args = (features(**STRONG), b5, flat_bars(60, 900_000), flat_bars(100, H1), flat_bars(100, 4 * H1), regime(),
            None, 102.0, 2)
    _, sigs, _ = se.evaluate("TSTUSDT", t, *args, dry_run=True)
    assert sigs and "TSTUSDT" not in se.last_signal_ms
    _, sigs2, _ = se.evaluate("TSTUSDT", t + M5, *args)
    assert sigs2                                       # a real pass right after still fires


def test_low_score_hard_pass_is_not_entry(cfg):
    se = SignalEngine(cfg)
    weak = features(rvol_5m=3.0, rs_1h=1.0, oi_chg_1h=2.0, taker_buy_ratio_15m=0.55, deep_wicks_24h=3,
                    bbw_pct_1h=40.0)
    row, sigs, _ = _eval(se, cfg, 1_000_000 * M5, f=weak)
    assert row.failed == []                              # every hard condition passes ...
    assert not sigs and row.score < cfg.score.min_entry  # ... but the score is too low


def test_score_calibration_anchor_points(cfg):
    """Documents the provisional calibration: bare minimum ~50, solid setup ~70+ in RISK_ON."""
    bare = features(rvol_5m=3.0, rs_1h=1.0, oi_chg_1h=2.0, taker_buy_ratio_15m=0.55, cvd_slope_1h=1.0,
                    funding_8h=0.0003)
    room = Headroom(4.0, None, True)
    s_bare, _ = compute_score(IGNITION, bare, "RISK_ON", room, cfg, set())
    s_solid, _ = compute_score(IGNITION, features(**STRONG), "RISK_ON", Headroom(5.0, None, True), cfg, set())
    assert 40 <= s_bare <= 55
    assert s_solid >= 70


def test_sessions_and_weekend(cfg):
    # 2026-09-26 is a Saturday; 14:00 UTC is New York session
    import datetime as dt
    ms = int(dt.datetime(2026, 9, 26, 14, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    name, tags = session_tags(ms, cfg)
    assert name == "NEW_YORK" and "WEEKEND" in tags


def test_watch_state_reported(cfg):
    se = SignalEngine(cfg)
    f = coil_features(rvol_5m=1.0)                       # ignition fails, coil watch holds
    row, sigs, watch = _eval(se, cfg, 1_000_000 * M5 + M5, f=f)
    assert row.state == WATCH and watch is not None and not sigs


def _swing_naive(h, n, lookback):
    start = max(n, len(h) - lookback)
    return [float(h[i]) for i in range(start, len(h) - n)
            if h[i] > h[i - n:i].max() and h[i] >= h[i + 1:i + n + 1].max()]


def _nodes_naive(b1h, lookback, bin_pct, window, min_ratio):
    import math
    h, l, v = b1h.h[-lookback:], b1h.l[-lookback:], b1h.v[-lookback:]
    lo, hi = float(l.min()), float(h.max())
    step = math.log1p(bin_pct / 100)
    nbins = int(math.ceil(math.log(hi / lo) / step)) + 1
    vol = np.zeros(nbins)
    for a, b, x in zip(np.floor(np.log(l / lo) / step).astype(int), np.floor(np.log(h / lo) / step).astype(int), v):
        b = min(b, nbins - 1)
        vol[a:b + 1] += x / (b - a + 1)
    th = min_ratio * vol[vol > 0].mean()
    return [lo * math.exp((i + 0.5) * step) for i in range(nbins)
            if vol[i] >= th and vol[i] >= vol[max(0, i - window): i + window + 1].max()]


@pytest.mark.parametrize("seed", range(8))
def test_vectorised_levels_match_reference(seed):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    bars = [Bar(i * H1, c[i], c[i] * (1 + abs(rng.normal(0, .005))), c[i] * (1 - abs(rng.normal(0, .005))), c[i],
                float(rng.uniform(1, 100)), 1, 0, 0, 1, (i + 1) * H1) for i in range(400)]
    b = BarArrays.from_bars(bars)
    assert swing_highs(b.h, 3, 336) == _swing_naive(b.h, 3, 336)
    assert volume_nodes(b, 336, 0.25, 3, 1.5) == pytest.approx(_nodes_naive(b, 336, 0.25, 3, 1.5))
