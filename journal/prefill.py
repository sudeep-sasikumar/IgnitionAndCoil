"""Entry-form prefill and checks (spec §8.2): stops recalculated from YOUR entry and leverage,
TP1/TP2 from the plan rules, estimated liquidation price, and warnings (never blocking)."""
from __future__ import annotations

import math

from plan.liquidation import Bracket, bracket_for, leverage_problem, liq_price_long


def _tick(x: float, precision: int | None, mode: str = "nearest") -> float:
    if precision is None:
        return float(f"{x:.8g}")
    step = 10.0 ** -precision
    q = x / step
    q = math.ceil(q - 1e-9) if mode == "ceil" else math.floor(q + 1e-9) if mode == "floor" else round(q)
    return round(q * step, max(precision, 0))


def compute(cfg, *, entry: float, margin: float, leverage: float, brackets: list[Bracket] | None,
            signal: dict | None, precision: int | None, stop_policy: str = "L",
            custom_stop: float | None = None) -> dict:
    t, p = cfg.trade, cfg.plan
    notional = margin * leverage
    qty = notional / entry
    warnings: list[str] = []
    if brackets:
        liq = liq_price_long(entry, qty, margin, brackets)
        mmr = bracket_for(brackets, notional).mmr
        lp = leverage_problem(brackets, notional, leverage)
        if lp:
            warnings.append(lp)
    else:
        mmr = 0.01
        liq = (qty * entry - margin) / (qty * (1 - mmr))
        warnings.append("risk brackets unavailable: liquidation estimated with MMR 1%")
    buf = t.liq_stop_buffer_pct / 100
    stop_l = _tick(liq * (1 + buf), precision, "ceil")

    plan = (signal or {}).get("plan") or {}
    s_raw = plan.get("structural_stop")
    if s_raw is None and plan.get("stop_s", {}).get("price") is not None:
        s_raw = plan["stop_s"]["price"]
    stop_s, stop_s_note = None, "no signal (own trade)" if not signal else "no structural stop"
    if s_raw is not None:
        s_raw = float(s_raw)
        if s_raw >= entry:
            stop_s_note = "structural stop is at/above your entry"
        elif s_raw < stop_l:
            stop_s_note = "beyond the liquidation-buffered stop at your leverage"
        else:
            stop_s, stop_s_note = s_raw, ""

    tp1 = _tick(entry * (1 + p.tp1_pct / 100), precision)
    tp2 = entry * (1 + p.tp2_pct / 100)
    res = (signal or {}).get("headroom", {}).get("level")
    if res:
        tp2 = min(tp2, float(res) * (1 - p.tp2_res_offset_pct / 100))
    tp2 = _tick(tp2, precision, "floor")
    if (tp2 / entry - 1) * 100 <= p.tp1_pct + p.tp2_min_gap_pct:
        tp2 = None

    chosen = {"L": stop_l, "S": stop_s}.get(stop_policy, custom_stop)
    if stop_policy == "custom":
        chosen = custom_stop
    chase = plan.get("chase_limit")
    if chase and entry > float(chase):
        warnings.append(f"entry {entry:g} is above the signal's don't-enter-above price {float(chase):g}")
    if chosen is not None:
        warnings += stop_warnings(cfg, chosen, entry, liq)
    elif stop_policy == "S":
        warnings.append(f"Policy S unavailable: {stop_s_note}")
    return {"notional": notional, "qty": qty, "liq_price": _tick(liq, precision), "mmr": mmr,
            "stop_l": stop_l, "stop_s": stop_s, "stop_s_note": stop_s_note, "stop": chosen,
            "tp1": tp1, "tp2": tp2, "warnings": warnings,
            "entry_gap_pct": (entry / float(plan["ref_entry"]) - 1) * 100 if plan.get("ref_entry") else None}


def stop_warnings(cfg, stop: float, entry: float, liq: float) -> list[str]:
    out = []
    buf_price = liq * (1 + cfg.trade.liq_stop_buffer_pct / 100)
    if stop <= liq:
        out.append(f"your stop {stop:g} is BEYOND the estimated liquidation price {liq:.6g} - you would be "
                   f"liquidated first")
    elif stop < buf_price:
        out.append(f"your stop {stop:g} is within {cfg.trade.liq_stop_buffer_pct}% of the liquidation price "
                   f"{liq:.6g}")
    return out
