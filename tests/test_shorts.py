"""Short side (backtest-only for now): mirrored setups, score, liquidation, plan, P&L and exits."""
import numpy as np
import pytest

from backtest.data import aggregate
from data.bars import BarArrays
from exchange.models import Bar
from exits.context import replay
from exits.engine import STOP_MOVE, make_params, new_state
from exits.short import make_params_short, price_range, replay_short
from levels.resistance import Headroom, Level, LevelMap, headroom_down
from plan.liquidation import bracket_for, liq_price_long, liq_price_short, parse_brackets
from plan.pnl import Leg, entry_fill, exit_fill, pnl_usd
from plan.trade_plan import build_plan
from signals.score import compute_score
from signals.setups import COIL_SHORT, IGNITION, IGNITION_SHORT, SHORT, CoilTracker, eval_ignition, eval_ignition_short
from tests.helpers import M5, breakout_bars, features

SOL = parse_brackets([
    {"bracket": 1, "notionalFloor": "0", "notionalCap": "2000000", "initialLeverage": 300, "maintMarginRatio": "0.002"},
    {"bracket": 2, "notionalFloor": "2000000", "notionalCap": "4700000", "initialLeverage": 200, "maintMarginRatio": "0.003"},
])
ROOM = Headroom(pct=6.0, level=Level(108.1, "swing_1h"), price_discovery=False)
ROOM_DOWN = Headroom(pct=6.0, level=Level(92.0, "swing_low_1h"), price_discovery=False)


def mirrored_features(**over):
    """features() reflected: passes every Ignition SHORT condition."""
    base = dict(price=98.0, ret_1h=-2.5, ret_4h=-0.5, ret_24h=-6.0, rs_1h=-2.0, vwap_24h=100.0, ema20_15m=99.0,
                ema50_15m=100.0, ema20_1h=99.5, ema50_1h=101.0, ema50_1h_rising=False, ema50_1h_falling=True,
                close_1h=99.0, taker_buy_ratio_15m=0.38, cvd_slope_1h=-1000.0, cvd_slope_1h_norm=-0.3,
                funding_8h=-0.00005, deep_wicks_24h=0, deep_upper_wicks_24h=0)
    base.update(over)
    return features(**base)


def breakdown_bars():
    """Mirror of breakout_bars around 100: flat bars (lows 99.5), then a breakdown candle."""
    b = breakout_bars()
    return BarArrays(b.t, 200 - b.o, 200 - b.l, 200 - b.h, 200 - b.c, b.v, b.qv, b.v - b.tbv, b.qv - b.tbqv, b.tc)


# ---- setups and score --------------------------------------------------------------

def test_ignition_short_passes_on_clean_breakdown(cfg):
    ev = eval_ignition_short(mirrored_features(), breakdown_bars(), ROOM_DOWN, cfg, set())
    assert ev.setup == IGNITION_SHORT
    assert ev.hard_pass, [c.name for c in ev.conds if not c.passed]
    assert ev.structural_stop == pytest.approx(99.7 + 0.1 * 0.8)          # candle high + 0.1 ATR15


@pytest.mark.parametrize("over,cond", [
    ({"ret_1h": -0.5}, "-8% <= ret_1h <= -1%"),
    ({"rs_1h": 0.5}, "rs_1h <= -1%"),
    ({"taker_buy_ratio_15m": 0.6}, "taker_sell_ratio_15m >= 0.55"),
    ({"cvd_slope_1h_norm": 0.2}, "cvd_slope_1h < 0"),
    ({"funding_8h": -0.001}, "funding_8h >= -0.03%"),
    ({"vwap_24h": 97.0}, "close < VWAP24h"),
])
def test_ignition_short_conditions_are_mirrored(cfg, over, cond):
    ev = eval_ignition_short(mirrored_features(**over), breakdown_bars(), ROOM_DOWN, cfg, set())
    failed = [c.name for c in ev.conds if not c.passed]
    assert not ev.hard_pass and cond.split()[0] in " ".join(failed), failed


def test_long_features_do_not_fire_a_short(cfg):
    assert not eval_ignition_short(features(), breakout_bars(), ROOM, cfg, set()).hard_pass


def test_short_score_equals_long_score_of_the_mirror_image(cfg):
    long_s, long_b = compute_score(IGNITION, features(), "RISK_ON", ROOM, cfg, set())
    short_s, short_b = compute_score(IGNITION_SHORT, mirrored_features(), "RISK_OFF", ROOM_DOWN, cfg, set())
    assert short_s == long_s and short_b == long_b
    # upper wicks are the short's squeeze risk; lower wicks don't count against it
    s_wicks, _ = compute_score(IGNITION_SHORT, mirrored_features(deep_upper_wicks_24h=3), "RISK_OFF", ROOM_DOWN, cfg, set())
    s_low, _ = compute_score(IGNITION_SHORT, mirrored_features(deep_wicks_24h=3), "RISK_OFF", ROOM_DOWN, cfg, set())
    assert s_wicks < short_s and s_low == short_s


def test_coil_short_watch_and_entry(cfg):
    tr = CoilTracker(cfg, SHORT)
    f = mirrored_features(bbw_pct_1h=5.0, ret_4h=-0.5, oi_chg_4h=4.0, funding_8h=0.0)
    h1 = BarArrays.from_bars([Bar(i * 3_600_000, 100, 100.5, 99.5, 100, 1, 100, 0.5, 50, 1, (i + 1) * 3_600_000)
                              for i in range(20)])
    conds, new = tr.update("X", 20 * 3_600_000, f, h1, set())
    assert new and all(c.passed for c in conds), [(c.name, c.passed) for c in conds]
    b15 = BarArrays.from_bars([Bar(0, 99.6, 99.7, 99.0, 99.2, 1, 99, 0.3, 30, 1, 900_000)])
    ev = tr.eval_entry("X", mirrored_features(rvol_15m=3.0), b15, ROOM_DOWN, set())
    assert ev.setup == COIL_SHORT and ev.hard_pass
    assert ev.structural_stop == pytest.approx(99.5 + cfg.plan.coil_stop_atr15 * 0.8)


def test_headroom_down_uses_nearest_support_below(cfg):
    lm = LevelMap([], [Level(95.0, "swing_low_1h"), Level(90.0, "low_7d"), Level(99.9, "hvn")])
    hr = headroom_down(lm, 100.0, 1.5, cfg)        # 99.9 is within ignore_within_pct -> skipped
    assert hr.level.price == 95.0 and hr.pct == pytest.approx(5.0)
    assert headroom_down(LevelMap([], []), 100.0, 1.5, cfg).price_discovery


# ---- liquidation, P&L, plan -----------------------------------------------------------

@pytest.mark.parametrize("lev", [5, 20, 50])
def test_short_liquidation_satisfies_the_balance_equation(lev):
    entry, margin = 100.0, 50.0
    qty = margin * lev / entry
    L = liq_price_short(entry, qty, margin, SOL)
    b = bracket_for(SOL, qty * entry)
    assert L > entry
    assert margin + qty * (entry - L) == pytest.approx(b.mmr * qty * L - b.cum)
    # mirror of the long: roughly the same distance on the other side
    assert (L / entry - 1) == pytest.approx(1 - liq_price_long(entry, qty, margin, SOL) / entry, rel=0.02)


def test_short_pnl_slippage_and_fees_work_against_you(cfg):
    t = cfg.trade
    fill = entry_fill(100.0, cfg, side="SHORT")
    assert fill < 100.0 and exit_fill(102.0, "stop", cfg, side="SHORT") > 102.0
    win = pnl_usd(100.0, 1.0, [Leg(97.0, 1.0, "tp")], cfg, side="SHORT")
    assert win == pytest.approx((fill - 97.0) - fill * t.taker_fee - 97.0 * t.maker_fee)
    loss = pnl_usd(100.0, 1.0, [Leg(102.0, 1.0, "stop")], cfg, side="SHORT")
    px = 102.0 * (1 + t.slippage_pct / 100)
    assert loss == pytest.approx((fill - px) - fill * t.taker_fee - px * t.taker_fee)


def test_short_plan(cfg):
    p = build_plan("SOLUSDT", IGNITION_SHORT, 100.0, 101.0, 95.0, SOL, cfg, side="SHORT")
    assert p.side == "SHORT" and p.tp1 == pytest.approx(97.0)
    assert p.liq_price > p.stop_l.price > 100.0                     # stop L sits inside liquidation
    assert p.stop_s.price == pytest.approx(101.0) and p.stop_s.loss_usd < 0
    assert p.tp2 == pytest.approx(95.0 * (1 + cfg.plan.tp2_res_offset_pct / 100))   # support caps TP2
    assert p.chase_limit < 100.0 and p.win_usd > 0
    assert build_plan("SOLUSDT", IGNITION_SHORT, 100.0, 99.5, None, SOL, cfg, side="SHORT").stop_s.price is None


# ---- exits: a short is the exact mirror image of a long ----------------------------------

T0 = 900_000 * 2_000_000


def _path(seed=7, n=160, pre=60):
    """5m path: `pre` quiet bars, then a trend up and a reversal; starts exactly at 100 at entry."""
    rng = np.random.default_rng(seed)
    rows, c = [], 100.0
    for i in range(pre + n):
        # quiet before entry, a trend (TP1, TP2, trailing) and then a reversal (trail / EMA exit)
        drift, noise = (0.0, 0.1) if i < pre else (0.25, 0.3) if i < pre + 50 else (-0.3, 0.3)
        o = c
        c = o * (1 + (drift + rng.normal(0, noise)) / 100)
        h, l = max(o, c) * (1 + abs(rng.normal(0, 0.1)) / 100), min(o, c) * (1 - abs(rng.normal(0, 0.1)) / 100)
        rows.append((o, h, l, c))
    shift = 100.0 - rows[pre - 1][3]            # the path starts exactly at the entry price
    return [tuple(x + shift for x in r) for r in rows], T0 + pre * M5


def _bars(rows, mirror=False):
    bars = []
    for i, (o, h, l, c) in enumerate(rows):
        if mirror:
            o, h, l, c = 200 - o, 200 - l, 200 - h, 200 - c
        bars.append(Bar(T0 + i * M5, o, h, l, c, 1, c, 0.5, c / 2, 1, T0 + (i + 1) * M5))
    return BarArrays.from_bars(bars)


@pytest.mark.parametrize("seed", [1, 7, 21])
def test_short_exits_are_the_exact_mirror_of_long_exits(cfg, seed):
    rows, entry_ms = _path(seed)
    E = 100.0
    long5, short5 = _bars(rows), _bars(rows, mirror=True)
    lst = new_state(make_params(E, entry_ms, 98.0, 103.0, 106.0, cfg))
    sst = new_state(make_params_short(E, entry_ms, 102.0, 97.0, 94.0, cfg))
    until = T0 + len(rows) * M5
    lev = replay(lst, long5, aggregate(long5, "15m"), cfg, until)
    sev = replay_short(sst, short5, aggregate(short5, "15m"), cfg, until)
    assert [e.kind for e in sev] == [e.kind for e in lev] and len(lev) > 2
    assert [e.ts for e in sev] == [e.ts for e in lev]
    for a, b in zip(lev, sev):
        assert b.price == pytest.approx(200 - a.price) and b.fraction == pytest.approx(a.fraction)
        if a.kind == STOP_MOVE:
            assert b.stop_after == pytest.approx(200 - a.stop_after)   # the short's stop only moves DOWN
    assert sst.exit_reason == lst.exit_reason and sst.remaining == pytest.approx(lst.remaining)
    lo, hi = price_range(sst)
    assert lo == pytest.approx(200 - lst.highest) and hi == pytest.approx(200 - lst.lowest)
