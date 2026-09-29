"""Trade plan for every ENTRY (spec §7): both stop policies, liquidation, chase limit,
TP1/TP2, $ P&L with fees, R-multiples and break-even win rate."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from plan.liquidation import Bracket, bracket_for, leverage_problem, liq_price_long
from plan.pnl import Leg, breakeven_winrate, pnl_usd


@dataclass
class StopPolicy:
    policy: str                 # "L" | "S"
    price: float | None         # None = n/a
    pct: float | None           # distance from reference entry, negative %
    loss_usd: float | None      # full position stopped out, fees + slippage included
    be_winrate: float | None
    tp1_r: float | None
    tp2_r: float | None
    win_r: float | None
    note: str = ""


@dataclass
class TradePlan:
    symbol: str
    setup: str
    ref_entry: float
    chase_limit: float
    margin: float
    leverage: float
    notional: float
    qty: float
    mmr: float
    liq_price: float
    stop_l: StopPolicy
    stop_s: StopPolicy
    tp1: float
    tp1_pct: float
    tp1_close_pct: float
    tp2: float | None
    tp2_pct: float | None
    tp2_close_pct: float
    runner_pct: float
    tp1_leg_usd: float
    tp2_leg_usd: float | None
    win_usd: float              # all targets hit (runner assumed to exit at TP2, or TP1 if no TP2)
    resistance: float | None
    price_discovery: bool
    warnings: list[str]
    structural_stop: float | None = None   # raw Policy S level, kept even when n/a at default size

    def as_dict(self) -> dict:
        return asdict(self)


def _round_sig(x: float) -> float:
    return float(f"{x:.6g}")


def _tick_round(x: float, precision: int | None, mode: str = "nearest") -> float:
    """Round to the symbol's price tick (10^-precision). ceil/floor for safety-critical prices."""
    if precision is None:
        return x
    step = 10.0 ** -precision
    q = x / step
    q = math.ceil(q - 1e-9) if mode == "ceil" else math.floor(q + 1e-9) if mode == "floor" else round(q)
    return round(q * step, max(precision, 0))


def build_plan(symbol: str, setup: str, ref: float, structural_stop: float | None,
               resistance: float | None, brackets: list[Bracket] | None, cfg,
               margin: float | None = None, leverage: float | None = None,
               precision: int | None = None) -> TradePlan:
    """precision = WEEX pricePrecision; prices are rounded to the tick (Policy L stop is
    rounded UP, i.e. away from liquidation)."""
    t, p = cfg.trade, cfg.plan
    if structural_stop is not None:
        structural_stop = _tick_round(structural_stop, precision, "floor")
    margin = t.margin_usd if margin is None else margin
    leverage = t.leverage if leverage is None else leverage
    notional = margin * leverage
    qty = notional / ref
    warnings: list[str] = []

    if brackets:
        liq = liq_price_long(ref, qty, margin, brackets)
        mmr = bracket_for(brackets, notional).mmr
        lp = leverage_problem(brackets, notional, leverage)
        if lp:
            warnings.append(lp)
    else:  # should not happen live; conservative fallback with a 1% MMR
        mmr = 0.01
        liq = (qty * ref - margin) / (qty * (1 - mmr))
        warnings.append("risk brackets unavailable: liquidation estimated with MMR 1%")

    buf = t.liq_stop_buffer_pct / 100
    stop_l_price = _tick_round(liq * (1 + buf), precision, "ceil")

    # Targets
    tp1 = _tick_round(ref * (1 + p.tp1_pct / 100), precision)
    tp2 = ref * (1 + p.tp2_pct / 100)
    if resistance is not None:
        tp2 = min(tp2, resistance * (1 - p.tp2_res_offset_pct / 100))
    tp2 = _tick_round(tp2, precision, "floor")
    tp2_pct = (tp2 / ref - 1) * 100
    tp2_close = p.tp2_close_pct
    runner = 100 - p.tp1_close_pct - p.tp2_close_pct
    if tp2_pct <= p.tp1_pct + p.tp2_min_gap_pct:
        tp2 = tp2_pct = None
        runner += tp2_close
        tp2_close = 0.0

    f1 = p.tp1_close_pct / 100
    tp1_leg = pnl_usd(ref, qty * f1, [Leg(tp1, 1.0, "tp")], cfg)
    tp2_leg = pnl_usd(ref, qty * tp2_close / 100, [Leg(tp2, 1.0, "tp")], cfg) if tp2 else None
    rest_px = tp2 if tp2 else tp1
    win = pnl_usd(ref, qty, [Leg(tp1, f1, "tp"), Leg(rest_px, 1 - f1, "tp")], cfg)
    full_tp1 = pnl_usd(ref, qty, [Leg(tp1, 1.0, "tp")], cfg)
    full_tp2 = pnl_usd(ref, qty, [Leg(tp2, 1.0, "tp")], cfg) if tp2 else None

    def policy(name: str, price: float | None, note: str = "") -> StopPolicy:
        if price is None:
            return StopPolicy(name, None, None, None, None, None, None, None, note)
        loss = pnl_usd(ref, qty, [Leg(price, 1.0, "stop")], cfg)
        risk = abs(loss) if loss < 0 else float("nan")
        return StopPolicy(name, _round_sig(price), (price / ref - 1) * 100, loss, breakeven_winrate(loss, win),
                          full_tp1 / risk, (full_tp2 / risk) if full_tp2 is not None else None, win / risk, note)

    stop_l = policy("L", stop_l_price)
    if structural_stop is None:
        stop_s = policy("S", None, "no structural stop")
    elif structural_stop >= ref:
        stop_s = policy("S", None, "structural stop at/above entry")
    elif structural_stop < stop_l_price:
        stop_s = policy("S", None, "beyond the liquidation-buffered stop")
    else:
        stop_s = policy("S", structural_stop)

    return TradePlan(
        symbol=symbol, setup=setup, ref_entry=ref,
        chase_limit=_round_sig(_tick_round(ref * (1 + t.max_chase_pct / 100), precision, "floor")),
        margin=margin, leverage=leverage, notional=notional, qty=qty, mmr=mmr,
        liq_price=_round_sig(_tick_round(liq, precision)),
        stop_l=stop_l, stop_s=stop_s,
        tp1=_round_sig(tp1), tp1_pct=p.tp1_pct, tp1_close_pct=p.tp1_close_pct,
        tp2=_round_sig(tp2) if tp2 else None, tp2_pct=tp2_pct, tp2_close_pct=tp2_close, runner_pct=runner,
        tp1_leg_usd=tp1_leg, tp2_leg_usd=tp2_leg, win_usd=win,
        resistance=resistance, price_discovery=resistance is None, warnings=warnings,
        structural_stop=structural_stop,
    )
