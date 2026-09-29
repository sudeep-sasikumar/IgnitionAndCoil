"""Exit engine: every rule of spec §7 on hand-built price paths."""
import json

import numpy as np
import pytest

from data.bars import BarArrays
from exchange.models import Bar
from exits.context import ctx_15m, last_swing_low, replay
from exits.engine import (EMA_EXIT, LIQUIDATION, MAX_HOLD, PHASE_2, PHASE_RUNNER, STOP, STOP_MOVE, TIME_STOP,
                          TP1, TP2, ExitState, make_params, new_state, on_bar_15m, on_prices, plan_legs)

M1 = 60_000
T0 = 1_000_000 * 300_000        # 5m-aligned entry time


def st(cfg, tp2=106.0, stop=98.0, liq=None):
    return new_state(make_params(100.0, T0, stop, 103.0, tp2, cfg, liq_price=liq))


def kinds(evs):
    return [e.kind for e in evs]


def test_params_from_config(cfg):
    p = make_params(100.0, T0, 98.0, 103.0, 106.0, cfg)
    assert p.tp1_frac == 0.4 and p.tp2_frac == 0.3
    assert p.be_stop == pytest.approx(100 * (1 + 2 * 0.0008 + 0.0003))   # BE + round-trip fees + slip
    assert p.time_stop_ms == T0 + 45 * M1 and p.time_stop_price == pytest.approx(101.0)
    assert p.max_hold_until_ms == T0 + 8 * 60 * M1
    assert make_params(100, T0, 98, 103, None, cfg).tp2_frac == 0.0


def test_initial_stop_and_stop_checked_before_targets(cfg):
    s = st(cfg)
    ev = on_prices(s, T0 + M1, trigger_low=97.9, high=103.5, last=99)   # both in one sample
    assert kinds(ev) == [STOP] and ev[0].price == 98.0 and ev[0].fraction == 1.0
    assert s.closed and s.exit_reason == STOP


def test_gap_through_stop_fills_at_open(cfg):
    s = st(cfg)
    ev = on_prices(s, T0 + 5 * M1, trigger_low=96.0, high=97.0, last=96.5, open_price=97.0)
    assert ev[0].price == 97.0


def test_tp1_moves_stop_to_breakeven_then_tp2_then_runner(cfg):
    s = st(cfg)
    ev = on_prices(s, T0 + 10 * M1, 100.5, 103.2, 103.0)
    assert kinds(ev) == [TP1, STOP_MOVE]
    assert s.phase == PHASE_2 and s.remaining == pytest.approx(0.6)
    assert s.stop == pytest.approx(s.p.be_stop) and s.tp1_ms == T0 + 10 * M1
    ev = on_prices(s, T0 + 20 * M1, 103.0, 106.1, 106.0)
    assert kinds(ev) == [TP2] and s.phase == PHASE_RUNNER and s.remaining == pytest.approx(0.3)
    # breakeven stop now protects the runner
    ev = on_prices(s, T0 + 30 * M1, s.p.be_stop - 0.01, 104, 101)
    assert kinds(ev) == [STOP] and ev[0].fraction == pytest.approx(0.3)
    legs = plan_legs(s)
    assert [(k, round(f, 2)) for _, f, k in legs] == [("tp", 0.4), ("tp", 0.3), ("stop", 0.3)]


def test_tp2_dropped_means_runner_60(cfg):
    s = st(cfg, tp2=None)
    on_prices(s, T0 + M1, 100, 103.5, 103)
    assert s.phase == PHASE_RUNNER and s.remaining == pytest.approx(0.6)


def test_time_stop_only_if_plus_1pct_not_reached(cfg):
    s = st(cfg)
    assert on_prices(s, T0 + 44 * M1, 99.5, 100.8, 100.5) == []
    ev = on_prices(s, T0 + 45 * M1, 99.5, 100.9, 100.4)
    assert kinds(ev) == [TIME_STOP] and ev[0].price == 100.4
    s2 = st(cfg)
    on_prices(s2, T0 + 10 * M1, 99.5, 101.2, 100.5)      # +1.2% reached early
    assert on_prices(s2, T0 + 50 * M1, 99.5, 100.5, 100.2) == []


def test_max_hold(cfg):
    s = st(cfg)
    on_prices(s, T0 + M1, 99.5, 101.5, 101)
    ev = on_prices(s, T0 + 8 * 60 * M1, 100.5, 101.8, 101.5)
    assert kinds(ev) == [MAX_HOLD] and ev[0].price == 101.5


def test_chandelier_trail_only_moves_up_and_only_after_tp1(cfg):
    s = st(cfg)
    assert on_bar_15m(s, T0 + 15 * M1, 101, 100, 0.5, 99) == []           # phase 1: stop fixed
    assert s.stop == 98.0
    on_prices(s, T0 + 16 * M1, 100.5, 104.0, 103.8)                         # TP1, highest 104
    ev = on_bar_15m(s, T0 + 30 * M1, 103.8, 102, 0.4, 101)
    # phase 2: max(BE+fees, 104 - 2.5*0.4 = 103.0) = 103.0
    assert kinds(ev) == [STOP_MOVE] and s.stop == pytest.approx(103.0)
    ev = on_bar_15m(s, T0 + 45 * M1, 103.5, 102, 1.0, 101)                 # chandelier 101.5 < 103 -> no move
    assert ev == [] and s.stop == pytest.approx(103.0)


def test_runner_trail_and_ema_exit(cfg):
    s = st(cfg)
    on_prices(s, T0 + M1, 100.5, 106.5, 106.2)                              # TP1 + TP2 -> runner
    assert s.phase == PHASE_RUNNER
    ev = on_bar_15m(s, T0 + 15 * M1, 106.0, 105.0, 0.4, 105.5)
    # max(chandelier 106.5-1.0=105.5, swing 105.5-0.04=105.46) = 105.5
    assert kinds(ev) == [STOP_MOVE] and s.stop == pytest.approx(105.5)
    ev = on_bar_15m(s, T0 + 30 * M1, 105.8, 106.0, 0.4, 105.5)               # close below EMA20
    assert kinds(ev) == [EMA_EXIT] and ev[0].price == 105.8 and s.closed


def test_trail_above_price_exits_at_close(cfg):
    s = st(cfg)
    on_prices(s, T0 + M1, 100.5, 110.0, 103.5)                              # huge spike then back
    ev = on_bar_15m(s, T0 + 15 * M1, 103.4, 100, 0.2, 100)                  # chandelier 109.5 > close
    assert kinds(ev) == [STOP] and ev[0].price == 103.4


def test_liquidation_when_stop_below_liq(cfg):
    s = new_state(make_params(100.0, T0, 97.0, 103.0, 106.0, cfg, liq_price=98.2))   # custom stop beyond liq
    ev = on_prices(s, T0 + M1, 98.5, 100.1, 98.6, mark_low=98.1)
    assert kinds(ev) == [LIQUIDATION] and ev[0].price == 98.2


def test_ignores_samples_before_entry_and_after_close(cfg):
    s = st(cfg)
    assert on_prices(s, T0, 90, 110, 100) == []
    on_prices(s, T0 + M1, 97, 99, 98)
    assert on_prices(s, T0 + 2 * M1, 90, 120, 100) == []


def test_state_json_roundtrip(cfg):
    s = st(cfg)
    on_prices(s, T0 + M1, 100.5, 103.5, 103)
    s2 = ExitState.from_dict(json.loads(json.dumps(s.to_dict())))
    assert s2 == s
    on_prices(s2, T0 + 2 * M1, 103, 106.5, 106)
    assert s2.phase == PHASE_RUNNER


def test_swing_low_and_ctx(cfg):
    l = np.array([5, 4, 3, 1, 3, 4, 5, 4, 3.5, 2, 3.0])   # pivot at i=3 confirmed; i=9 lacks 3 bars after
    assert last_swing_low(l, 3) == 1.0
    bars = [Bar(i * 900_000, 100, 101, 99, 100 + (i % 3), 1, 100, 0.5, 50, 1, (i + 1) * 900_000) for i in range(60)]
    close, ema, atr, sw = ctx_15m(BarArrays.from_bars(bars), cfg)
    assert close == 100 + 59 % 3 and ema is not None and atr > 0


def _bars(step, rows):
    return BarArrays.from_bars([Bar(T0 + i * step, o, h, l, c, 1, c, 0.5, c / 2, 1, T0 + (i + 1) * step)
                                for i, (o, h, l, c) in enumerate(rows)])


def test_replay_over_5m_bars_matches_manual_steps(cfg):
    rows = [(100, 100.6, 99.8, 100.4), (100.4, 103.3, 100.2, 103.0), (103, 103.4, 102.5, 103.1),
            (103.1, 106.2, 103, 105.9), (105.9, 106, 101, 101.2)]
    b5 = _bars(300_000, rows)
    b15 = _bars(900_000, [(100, 103.4, 99.8, 103.1), (103.1, 106.2, 101, 101.2)])
    s = st(cfg)
    seen = []
    ev = replay(s, b5, b15, cfg, until_ms=T0 + 25 * M1, on_bar=lambda t, lo, hi: seen.append(t))
    # bar 2 hits TP1 (stop -> BE ~100.19), bar 4 hits TP2, bar 5's low 101 stays above BE;
    # only one 15m bar closed so ATR/EMA are not available yet -> no trail, runner still open
    assert kinds(ev) == [TP1, STOP_MOVE, TP2] and not s.closed and s.remaining == pytest.approx(0.3)
    assert s.tp1_ms == T0 + 10 * M1 and s.tp2_ms == T0 + 20 * M1
    assert seen == [T0 + k * 5 * M1 for k in range(1, 6)]
    # replay is idempotent: nothing new when called again
    assert replay(s, b5, b15, cfg, until_ms=T0 + 25 * M1) == []
