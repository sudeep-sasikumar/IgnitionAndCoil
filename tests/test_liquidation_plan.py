"""Liquidation price (calibrated to WEEX), size linking, trade plan maths."""
import pytest

from plan.liquidation import bracket_for, leverage_problem, link_size, liq_price_long, parse_brackets
from plan.pnl import Leg, breakeven_winrate, pnl_usd
from plan.trade_plan import build_plan

INX = parse_brackets([
    {"bracket": 1, "notionalFloor": "0", "notionalCap": "10000", "initialLeverage": 50, "maintMarginRatio": "0.01"},
    {"bracket": 2, "notionalFloor": "10000", "notionalCap": "50000", "initialLeverage": 20, "maintMarginRatio": "0.025"},
    {"bracket": 3, "notionalFloor": "50000", "notionalCap": "300000", "initialLeverage": 10, "maintMarginRatio": "0.05"},
])
SOL = parse_brackets([
    {"bracket": 1, "notionalFloor": "0", "notionalCap": "2000000", "initialLeverage": 300, "maintMarginRatio": "0.002"},
    {"bracket": 2, "notionalFloor": "2000000", "notionalCap": "4700000", "initialLeverage": 200, "maintMarginRatio": "0.003"},
])


def test_weex_screenshot_case_inx_3x():
    # Real WEEX position (M0): INXUSDT long 3x, qty 45,600, entry 0.006566, margin 99.8076 -> WEEX liq 0.004421
    liq = liq_price_long(0.006566, 45_600, 99.8076, INX)
    assert liq == pytest.approx(0.004421, abs=0.000001)


@pytest.mark.parametrize("lev", [5, 10, 20, 50, 100, 200])
def test_liq_bracket1_closed_form(lev):
    # With no cum in bracket 1: L = E (1 - 1/lev) / (1 - mmr)
    e, notional = 142.30, 1000.0
    liq = liq_price_long(e, notional / e, notional / lev, SOL)
    assert liq == pytest.approx(e * (1 - 1 / lev) / (1 - 0.002), rel=1e-12)
    assert liq < e


def test_liq_monotonic_in_leverage():
    e = 50.0
    liqs = [liq_price_long(e, 1000 / e, 1000 / lev, INX) for lev in (2, 5, 10, 25, 50)]
    assert liqs == sorted(liqs)


def test_cum_makes_maintenance_margin_continuous():
    b1, b2 = INX[0], INX[1]
    n = 10_000.0
    assert n * b1.mmr - b1.cum == pytest.approx(n * b2.mmr - b2.cum)
    assert bracket_for(INX, 60_000).mmr == 0.05


def test_leverage_limit():
    assert leverage_problem(INX, 1000, 50) is None
    assert "exceeds" in leverage_problem(INX, 20_000, 50)


def test_link_size():
    assert link_size(20, 50, 999, "margin") == (20, 50, 1000)
    assert link_size(20, 25, 999, "leverage") == (20, 25, 500)
    assert link_size(999, 50, 1500, "notional") == (30, 50, 1500)
    with pytest.raises(ValueError):
        link_size(20, 0, 1000, "margin")


def test_pnl_fees_and_slippage(cfg):
    # $1000 notional, +3% TP (maker, no slip) vs entry with 0.03% slip and taker fee
    q = 1000 / 100
    fill = 100 * 1.0003
    expected = q * (103 - fill) - q * fill * 0.0008 - q * 103 * 0.0002
    assert pnl_usd(100, q, [Leg(103, 1.0, "tp")], cfg) == pytest.approx(expected)
    stop = pnl_usd(100, q, [Leg(98, 1.0, "stop")], cfg)
    px = 98 * (1 - 0.0003)
    assert stop == pytest.approx(q * (px - fill) - q * fill * 0.0008 - q * px * 0.0008)
    assert breakeven_winrate(-10, 30) == pytest.approx(0.25)


def test_plan_policies_targets_and_breakeven(cfg):
    p = build_plan("SOLUSDT", "IGNITION", 142.30, 141.05, None, SOL, cfg, margin=20, leverage=50, precision=2)
    assert p.notional == 1000 and p.leverage == 50
    assert p.liq_price == pytest.approx(142.30 * 0.98 / 0.998, abs=0.01)
    assert p.stop_l.price >= p.liq_price * 1.0015 - 1e-9          # buffer kept, rounded up
    assert p.stop_s.price == 141.05 and p.stop_s.loss_usd > p.stop_l.loss_usd
    assert p.chase_limit == pytest.approx(143.01)
    assert p.tp1 == pytest.approx(146.57) and p.tp2 == pytest.approx(150.83)   # no resistance: +3%, +6%
    assert p.price_discovery and p.runner_pct == 30
    for sp in (p.stop_l, p.stop_s):
        assert sp.be_winrate == pytest.approx(abs(sp.loss_usd) / (abs(sp.loss_usd) + p.win_usd))
        assert sp.win_r == pytest.approx(p.win_usd / abs(sp.loss_usd))
    # the spec's illustrative magnitudes: L stop ~ -$17-19, S stop ~ -$10
    assert -20 < p.stop_l.loss_usd < -15 and -12 < p.stop_s.loss_usd < -9


def test_structural_stop_beyond_policy_l_is_na(cfg):
    p = build_plan("X", "IGNITION", 100.0, 97.0, None, SOL, cfg, margin=20, leverage=50)   # 50x liq ~ -1.8%, so -3% is beyond it
    assert p.stop_s.price is None and "beyond" in p.stop_s.note
    assert p.stop_l.price is not None


def test_tp2_capped_by_resistance_or_dropped(cfg):
    p = build_plan("X", "IGNITION", 100.0, 99.5, 105.5, SOL, cfg, precision=2)
    assert p.tp2 == pytest.approx(105.28)                       # 105.5 - 0.2%, floored to tick
    assert p.runner_pct == 30
    p2 = build_plan("X", "IGNITION", 100.0, 99.5, 104.1, SOL, cfg, precision=2)
    assert p2.tp2 is None and p2.runner_pct == 60 and p2.tp2_close_pct == 0   # 103.89 <= TP1 + 1%


def test_high_mmr_coin_has_tight_policy_l(cfg):
    p = build_plan("INXUSDT", "IGNITION", 0.0065, 0.00645, None, INX, cfg, margin=20, leverage=50, precision=6)
    assert -1.05 < (p.liq_price / 0.0065 - 1) * 100 < -0.95          # ~ -1.0% at 50x with MMR 1%
    assert p.stop_l.pct > -0.9
