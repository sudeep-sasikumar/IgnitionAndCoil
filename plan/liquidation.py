"""Liquidation price and position sizing (isolated margin, USDT-M, long).

Formula calibrated against a real WEEX position in M0 (INXUSDT, 3x): WEEX's displayed
liquidation price satisfies
    margin + qty * (L - entry) = MMR * qty * L - cum
    => L = (qty*entry - margin - cum) / (qty * (1 - MMR))
where MMR/cum come from the notional bracket (riskLimits). Fees are NOT in WEEX's
displayed liq price. Liquidation triggers on MARK price.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bracket:
    floor: float
    cap: float
    max_leverage: int
    mmr: float
    cum: float      # maintenance amount deduction, derived (WEEX does not publish it)


def parse_brackets(raw: list[dict]) -> list[Bracket]:
    rows = sorted(raw, key=lambda b: int(b["bracket"]))
    out: list[Bracket] = []
    cum, prev_mmr = 0.0, 0.0
    for b in rows:
        floor, mmr = float(b["notionalFloor"]), float(b["maintMarginRatio"])
        cum += floor * (mmr - prev_mmr)
        out.append(Bracket(floor, float(b["notionalCap"]), int(b["initialLeverage"]), mmr, cum))
        prev_mmr = mmr
    return out


def parse_risk_limits(rows: list[dict]) -> dict[str, list[Bracket]]:
    return {r["symbol"]: parse_brackets(r["brackets"]) for r in rows if r.get("brackets")}


def bracket_for(brackets: list[Bracket], notional: float) -> Bracket:
    for b in brackets:
        if b.floor <= notional < b.cap:
            return b
    return brackets[-1]


def liq_price_long(entry: float, qty: float, margin: float, brackets: list[Bracket]) -> float:
    b = bracket_for(brackets, qty * entry)
    liq = (qty * entry - margin - b.cum) / (qty * (1 - b.mmr))
    return max(liq, 0.0)


def liq_price_short(entry: float, qty: float, margin: float, brackets: list[Bracket]) -> float:
    """Mirror of liq_price_long from the same balance equation:
        margin + qty * (entry - L) = MMR * qty * L - cum
        => L = (qty*entry + margin + cum) / (qty * (1 + MMR))
    NOT yet checked against a real WEEX short (the long formula was calibrated in M0)."""
    b = bracket_for(brackets, qty * entry)
    return (qty * entry + margin + b.cum) / (qty * (1 + b.mmr))


def leverage_problem(brackets: list[Bracket], notional: float, leverage: float) -> str | None:
    b = bracket_for(brackets, notional)
    if leverage > b.max_leverage:
        return f"leverage {leverage:g}x exceeds the {b.max_leverage}x max for this size"
    return None


# ---- margin / leverage / notional linking (dashboard form, spec §8.2) ----------

def link_size(margin: float, leverage: float, notional: float, edited: str) -> tuple[float, float, float]:
    """Editing margin or leverage recalculates notional; editing notional recalculates
    margin at the current leverage."""
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    if edited in ("margin", "leverage"):
        return margin, leverage, margin * leverage
    if edited == "notional":
        return notional / leverage, leverage, notional
    raise ValueError(f"unknown field {edited}")
